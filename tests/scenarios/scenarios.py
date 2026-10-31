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
    def __init__(self, name: str, live: dict | None = None):
        """live={"armed": bool, "paper": bool}: a live drill against the fake Groww exchange (the paper
        strategy is dropped unless paper=True, so the two ledgers can be told apart)."""
        self.name, self.live = name, live
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
        if live:
            if not live.get("paper"):
                self.cfg["paper"] = []
            self.cfg["live"] = {"enabled": True, "armed": live.get("armed", True), "db": str(self.dir / "live.db"),
                                "strategies": [str(ROOT / "strategies/put_credit_spread.yaml")], "max_lots": 1,
                                **{k: v for k, v in live.items() if k.startswith("margin_")}}
        (self.dir / "config.yaml").write_text(yaml.safe_dump(self.cfg))
        self.log, self.codes = "", []

    # --- actions -----------------------------------------------------------------------
    def day(self, date: str = MON, until: str = "10:00", **spec) -> None:
        spec = {"date": date, "until": until, **({"groww": True} if self.live else {}), **spec}
        p = subprocess.run([sys.executable, str(Path(__file__).parent / "harness.py"), str(self.dir), json.dumps(spec)],
                           cwd=ROOT, capture_output=True, text=True, timeout=400)
        self.codes.append(p.returncode)
        self.log += f"\n=== {date} ===\n{p.stdout}{p.stderr}"

    def settle(self, close: float, day: str = TUE) -> list[str]:
        strategies = self.cfg["live"]["strategies"] if self.live else self.cfg["paper"]
        out = settle({"paper": strategies}, self.store(), FakeApi(close), date.fromisoformat(day))
        if self.live:                                     # Groww drops expired contracts
            d = self.groww()
            d["net"] = {k: v for k, v in d["net"].items() if not k.startswith("NIFTY26N03")}
            (self.dir / "groww.json").write_text(json.dumps(d))
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
        return PaperStore(self.dir / ("live.db" if self.live else "paper.db"))

    def groww(self) -> dict:
        """The fake exchange's state: placed (every order sent), net positions, orders."""
        return json.loads((self.dir / "groww.json").read_text())

    def sent(self) -> list[tuple]:
        return [(o["symbol"], o["side"], o["type"]) for o in self.groww()["placed"]]

    def max_tick_gap_s(self, instrument: str = "NSE|NIFTY|IDX") -> float:
        """Longest simulated silence between two recorded NIFTY ticks: a stalled feed handler shows up here."""
        import sqlite3

        ts = [t for (t,) in sqlite3.connect(self.dir / "ticks.db").execute(
            "SELECT ts_ms FROM ltp WHERE instrument = ? ORDER BY ts_ms", (instrument,))]
        return max(b - a for a, b in zip(ts, ts[1:])) / 1000

    def fix_groww(self) -> None:
        """Ranga sorts out the account: stray positions and foreign orders removed."""
        d = self.groww()
        d["net"] = {k: v for k, v in d["net"].items() if not k.startswith("NIFTY26N0320000")}
        d["orders"] = {k: v for k, v in d["orders"].items() if v["order_reference_id"] != "manualorder1"}
        (self.dir / "groww.json").write_text(json.dumps(d))

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


# --- live drills: the same days against a fake Groww exchange ---------------------------------

LIVE = {"armed": True}


def live_normal_week(r: Run):
    r.day(MON, "10:00")
    r.day(TUE, "09:45")
    r.settle(25000.0)


def live_unarmed(r: Run):
    r.day(MON, "09:45")


def live_dead_feed(r: Run):
    r.day(MON, "10:00", silent=["09:40", "09:50"])


def live_manual_kill(r: Run):
    r.day(MON, "09:50", cli_kill_at="09:35")


def live_startup_mismatch(r: Run):
    r.day(MON, "09:45")
    r.day(TUE, "09:30", groww_stray={"20000": 65})
    r.after_mismatch = r.kill()
    r.fix_groww()
    r.resume()
    r.day(TUE, "09:30")


def live_foreign_order(r: Run):
    r.day(MON, "09:45", groww_foreign_order=True)
    r.after_halt = r.kill()


def live_groww_outage(r: Run):
    r.day(MON, "10:10", groww_outage=["09:40", "09:50"])


def live_recorder_crash(r: Run):
    r.day(MON, "10:00", crash_at="09:40")
    r.alert = r.heartbeat_check(MON, "09:50")
    r.day(TUE, "09:30")


def live_lost_response(r: Run):
    r.day(MON, "09:50", groww_timeout_lands=True)


def live_margin_short(r: Run):
    r.day(MON, "09:50", groww_available=70_000)          # enough to buy the protection, not to carry the short


def live_margin_unreadable(r: Run):
    r.day(MON, "09:50", groww_margin_down=True)


def live_hung_groww(r: Run):
    r.day(MON, "10:00", groww_hang_seconds=1.5)          # 90 simulated seconds


def live_and_paper_together(r: Run):
    r.day(MON, "10:00")
    r.day(TUE, "09:45")


SCENARIOS = {f.__name__: f for f in (normal_week, dead_feed, crash_move, manual_kill, startup_mismatch,
                                     recorder_crash, no_protection_liquidity, expiry_far_below)}
LIVE_SCENARIOS = {f.__name__: f for f in (live_normal_week, live_unarmed, live_dead_feed, live_manual_kill,
                                          live_startup_mismatch, live_foreign_order, live_groww_outage,
                                          live_recorder_crash, live_lost_response, live_and_paper_together,
                                          live_margin_short, live_margin_unreadable, live_hung_groww)}
LIVE_FLAGS = {"live_unarmed": {"armed": False}, "live_and_paper_together": {"armed": True, "paper": True},
              "live_margin_short": {"margin_min_free": 0}}       # the entry floor off, so the protection does get bought
SCENARIOS |= LIVE_SCENARIOS


def run_all(names=None) -> dict[str, Run]:
    from concurrent.futures import ThreadPoolExecutor

    names = names or list(SCENARIOS)

    def go(name):
        r = Run(name, {**LIVE, **LIVE_FLAGS.get(name, {})} if name in LIVE_SCENARIOS else None)
        SCENARIOS[name](r)
        return name, r

    with ThreadPoolExecutor(len(names)) as pool:
        return dict(pool.map(go, names))
