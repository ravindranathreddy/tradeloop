"""Watchdog (design rule 12): plain code, no LLM, that triggers the kill switch on hard rules.

Inside the recorder a thread checks every few seconds while the market is open:

  stale data         positions are open and no tick for `stale_seconds`
  max open loss      a strategy's open positions, marked at the last price, lose more than
                     `max_open_loss` rupees
  untagged position  the broker holds a position the ledger doesn't know (Rule #1); needs two
                     checks in a row, since a fill reaches the broker a moment before the ledger

It writes a heartbeat file each round. A second check, run by cron every minute, covers the
case the thread can't: the recorder itself dying. A stale heartbeat in session with positions
open triggers the kill switch too. Without open positions it only prints an alert.

    python -m tradeloop.watchdog            # heartbeat check (cron)

A breach calls `kill_switch.request`; the paper trader then halts and flattens. The reason is
logged as an ALERT (phone alerts come later). Lift the halt with `kill_switch resume`.
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import yaml

from tradeloop.core.market_calendar import IST, MarketCalendar, UnknownYear, default_calendar
from tradeloop.core.types import Tick
from tradeloop.kill_switch import KILL_FILE, active, request

log = logging.getLogger("watchdog")
HEARTBEAT_FILE = Path("data/heartbeat")
HEARTBEAT_MAX_AGE_S = 60


def _wall_ms() -> int:
    return int(time.time() * 1000)


def in_session(cal: MarketCalendar, now_ms: int) -> bool:
    try:
        return cal.is_open(datetime.fromtimestamp(now_ms / 1000, IST))
    except UnknownYear:
        return False


class Watchdog:
    def __init__(self, traders: list, *, kill_file: Path = KILL_FILE, heartbeat_file: Path = HEARTBEAT_FILE,
                 stale_seconds: float = 120, max_open_loss: float = 20_000, interval: float = 5,
                 calendar: MarketCalendar | None = None, clock_ms: Callable[[], int] = _wall_ms):
        self.traders, self.kill_file, self.heartbeat_file = traders, Path(kill_file), Path(heartbeat_file)
        self.stale_ms, self.max_open_loss, self.interval = int(stale_seconds * 1000), max_open_loss, interval
        self.cal, self.clock = calendar or default_calendar(), clock_ms
        self.last_tick_ms = clock_ms()
        self._untagged_before: set = set()

    def on_tick(self, _tick: Tick) -> None:
        self.last_tick_ms = self.clock()

    def check(self) -> list[str]:
        now = self.clock()
        if not in_session(self.cal, now):
            self._untagged_before = set()
            return []
        out = []
        held = [(t, t.ledger.positions()) for t in self.traders]
        if any(p for _, p in held):
            opened = datetime.fromtimestamp(now / 1000, IST).date()
            since = max(self.last_tick_ms, int(self.cal.session(opened).open.timestamp() * 1000))
            if now - since > self.stale_ms:
                out.append(f"stale data: no tick for {(now - since) / 1000:.0f}s with positions open")
        for t, positions in held:
            loss = -sum(p.qty * (t.quotes[p.instrument].close - p.avg_price)
                        for p in positions if p.instrument in t.quotes)
            if loss > self.max_open_loss:
                out.append(f"max loss: {t.strategy_id} is down Rs {loss:,.0f} on open positions "
                           f"(limit Rs {self.max_open_loss:,.0f})")
        known = {(t.strategy_id, p.instrument) for t, positions in held for p in positions}
        unknown = {(t.strategy_id, p.instrument) for t in self.traders for p in t.broker.positions()} - known
        for strategy_id, instrument in sorted(unknown & self._untagged_before, key=str):
            out.append(f"untagged position: {instrument} at the broker, unknown to {strategy_id} (Rule #1)")
        self._untagged_before = unknown
        return out

    def run_once(self) -> list[str]:
        self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_file.write_text(str(self.clock()))
        breaches = self.check()
        if breaches and active(self.kill_file) is None:
            for b in breaches:
                log.error("ALERT watchdog: %s", b)
            request(self.kill_file, "watchdog: " + "; ".join(breaches), self.clock())
        return breaches

    def start(self) -> threading.Thread:
        def loop():
            while True:
                time.sleep(self.interval)
                try:
                    self.run_once()
                except Exception:           # a bug here must not stop the recording; the heartbeat check covers it
                    log.exception("Watchdog round failed")
        thread = threading.Thread(target=loop, name="watchdog", daemon=True)
        thread.start()
        return thread


# --- the second check, from cron ----------------------------------------------------------

def check_heartbeat(cfg: dict, *, now_ms: int | None = None, cal: MarketCalendar | None = None) -> str | None:
    """The recorder is silent during the session. Returns the alert text, or None if all is well."""
    from tradeloop.paper_store import PaperStore

    now = now_ms or _wall_ms()
    cal = cal or default_calendar()
    if not in_session(cal, now):
        return None
    wd = cfg.get("watchdog", {})
    path = Path(wd.get("heartbeat_file", HEARTBEAT_FILE))
    try:
        age = (now - int(path.read_text())) / 1000
    except (FileNotFoundError, ValueError):
        age = float("inf")
    if age <= HEARTBEAT_MAX_AGE_S:
        return None
    store = PaperStore(cfg.get("paper_db", "data/paper.db"))
    held = []
    for p in cfg.get("paper", []):
        sid = yaml.safe_load(Path(p).read_text())["strategy_id"]
        held += store.rebuild_ledger(sid).positions()
    text = f"watchdog heartbeat lost ({age:.0f}s old); recorder is not running"
    kill_file = Path(cfg.get("kill_file", KILL_FILE))
    if held and active(kill_file) is None:
        request(kill_file, text, now)
        text += f"; {len(held)} positions open, kill switch requested"
    return "ALERT " + text


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    a = p.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())
    alert = check_heartbeat(cfg)
    if alert:
        print(datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), alert)       # cron mails any output


if __name__ == "__main__":
    main()
