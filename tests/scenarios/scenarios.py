"""Scenario drills: the recorder, paper trader, risk gate, watchdog, kill switch and
reconciliation running together on simulated days (see harness.py). Each scenario is a
function that drives a `Run`; tests/test_scenarios.py checks what is left behind.

    RUN_SCENARIOS=1 pytest tests/test_scenarios.py        (about 2 minutes: they run in parallel)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import date, datetime, time as dtime
from pathlib import Path

import yaml

from tests.test_paper import FakeApi
from tradeloop.core.market_calendar import IST
from tradeloop.kill_switch import active, clear
from tradeloop.paper import settle
from tradeloop.paper_store import PaperStore
from tradeloop.watchdog import check_heartbeat

ROOT = Path(__file__).resolve().parents[2]
MON, TUE = "2026-11-02", "2026-11-03"
SID = "put_credit_spread"


class Run:
    def __init__(self, name: str):
        self.name = name
        self.dir = Path(tempfile.mkdtemp(prefix=f"drill_{name}_"))
        self.kill_file = self.dir / "KILL"
        self.cfg = {
            "db_path": str(self.dir / "ticks.db"), "end_time": "15:41", "poll_seconds": 0.5, "stale_seconds": 10,
            "min_free_gb": 1, "record_depth": True,
            "underlyings": [{"name": "NIFTY", "exchange": "NSE", "strikes_each_side": 5, "recenter_points": 200}],
            "paper": [str(ROOT / "strategies/put_credit_spread.yaml")],
            "paper_db": str(self.dir / "paper.db"), "paper_max_lots": 1, "kill_file": str(self.kill_file),
            "watchdog": {"stale_seconds": 120, "max_open_loss": 20000, "interval": 0.2,
                         "heartbeat_file": str(self.dir / "hb")},
        }
        (self.dir / "config.yaml").write_text(yaml.safe_dump(self.cfg))
        self.log, self.codes = "", []

    # --- actions -----------------------------------------------------------------------
    def day(self, date: str = MON, until: str = "10:00", **spec) -> None:
        spec = {"date": date, "until": until, **spec}
        p = subprocess.run([sys.executable, str(Path(__file__).parent / "harness.py"), str(self.dir), json.dumps(spec)],
                           cwd=ROOT, capture_output=True, text=True, timeout=400)
        self.codes.append(p.returncode)
        self.log += f"\n=== {date} ===\n{p.stdout}{p.stderr}"

    def settle(self, close: float, day: str = TUE) -> list[str]:
        out = settle({"paper": self.cfg["paper"]}, self.store(), FakeApi(close), date.fromisoformat(day))
        self.log += "\n" + "\n".join(out)
        return out

    def resume(self) -> None:
        clear(self.kill_file)

    def heartbeat_check(self, day: str, hm: str) -> str | None:
        h, m = map(int, hm.split(":"))
        now = int(datetime.combine(date.fromisoformat(day), dtime(h, m), IST).timestamp() * 1000)
        return check_heartbeat(self.cfg, now_ms=now)

    # --- what is left behind ---------------------------------------------------------------
    def store(self) -> PaperStore:
        return PaperStore(self.dir / "paper.db")

    def fills(self) -> list[tuple]:
        return self.store().query("SELECT instrument, side, qty, price FROM fills ORDER BY ts_ms, rowid")

    def order_statuses(self) -> list[str]:
        return [s for (s,) in self.store().query("SELECT status FROM orders ORDER BY placed_ms, rowid")]

    def ledger(self):
        return self.store().rebuild_ledger(SID)

    def kill(self) -> dict | None:
        return active(self.kill_file)


# --- scenarios -----------------------------------------------------------------------------

def normal_week(r: Run):
    r.day(MON, "10:00")
    r.day(TUE, "09:45")
    r.settle(25000.0)


def dead_feed(r: Run):
    r.day(MON, "10:00", silent=["09:40", "09:50"])


def crash_move(r: Run):
    r.day(MON, "10:00", moves=[["09:40", 24450.0, {"24400": 400.0, "23900": 60.0}]])


def manual_kill(r: Run):
    r.day(MON, "09:50", cli_kill_at="09:35")


def startup_mismatch(r: Run):
    r.day(MON, "09:45")
    r.day(TUE, "09:30", stray=True)
    r.after_mismatch = r.kill()
    r.resume()
    r.day(TUE, "09:30")


def recorder_crash(r: Run):
    r.day(MON, "10:00", crash_at="09:40")
    r.alert = r.heartbeat_check(MON, "09:50")
    r.day(TUE, "09:30")


def no_protection_liquidity(r: Run):
    r.day(MON, "09:45", no_book=[23900])


def expiry_far_below(r: Run):
    r.day(MON, "09:45")
    r.day(TUE, "09:30")
    r.settle(23500.0)


SCENARIOS = {f.__name__: f for f in (normal_week, dead_feed, crash_move, manual_kill, startup_mismatch,
                                     recorder_crash, no_protection_liquidity, expiry_far_below)}


def run_all(names=None) -> dict[str, Run]:
    from concurrent.futures import ThreadPoolExecutor

    names = names or list(SCENARIOS)

    def go(name):
        r = Run(name)
        SCENARIOS[name](r)
        return name, r

    with ThreadPoolExecutor(len(names)) as pool:
        return dict(pool.map(go, names))
