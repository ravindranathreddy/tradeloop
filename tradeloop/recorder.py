"""Tick recorder: index values, option LTPs and top-of-book for the nearest weekly expiry.

Run every weekday (cron at ~9:05 IST); on holidays it exits at once. It logs in,
checks Groww's listed expiries against the market calendar, subscribes to the
indices, and on the first index value picks strikes around ATM. The feed
publishes Ticks on the bus; TickWriter stores them. Strikes are re-centred if
spot drifts far. Exits at the configured end time.

Disk guard: it won't start with less than `min_free_gb` free where the database
lives, and stops cleanly (exit code 1) if free space drops below that mid-day.
A full disk would break SQLite mid-session and the rest of the laptop with it.

    python -m tradeloop.recorder --config config.yaml
"""
import argparse
import logging
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from tradeloop.core.bus import Bus, Topic
from tradeloop.core.ids import InstrumentId
from tradeloop.core.interfaces import MarketData
from tradeloop.core.market_calendar import MarketCalendar, UnknownYear
from tradeloop.core.types import Contract, Tick
from tradeloop.instruments import pick_options
from tradeloop.store import Store, TickWriter

IST = ZoneInfo("Asia/Kolkata")
GB = 1024 ** 3
DISK_CHECK_SECONDS = 60
log = logging.getLogger("recorder")


def free_bytes(path: str | Path) -> int:
    """Free space on the disk that holds `path` (which may not exist yet)."""
    p = Path(path).resolve().parent
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free


def low_disk(path: str | Path, min_free_gb: float, free=free_bytes) -> str | None:
    """A message if free space is below min_free_gb, else None."""
    have = free(path) / GB
    return f"only {have:.1f} GB free for {path}, need {min_free_gb:g} GB" if have < min_free_gb else None


class Recorder:
    def __init__(self, cfg: dict, md: MarketData, store: Store, today: date,
                 free: Callable[[str | Path], int] = free_bytes, clock: Callable[[], float] = time.monotonic):
        self.md, self.store, self.today = md, store, today
        self.min_free_gb = float(cfg.get("min_free_gb", 5))
        self.free, self.clock = free, clock
        self._disk_checked = float("-inf")
        self.stopped_for_disk = False
        self.bus = Bus()
        self.writer = TickWriter(store)
        self.underlyings = {InstrumentId.index(u["exchange"], u["name"]): u for u in cfg["underlyings"]}
        self.centred_at: dict[InstrumentId, float] = {}   # index -> spot used for the last selection
        self.subscribed: set[InstrumentId] = set()
        md.on_tick(lambda t: self.bus.publish(Topic.TICK, t))
        self.bus.subscribe(Topic.TICK, self.writer.on_tick)
        self.bus.subscribe(Topic.TICK, self.on_tick)

    def start(self):
        self.md.subscribe(self.underlyings)

    def check_disk(self):
        """Stop recording if the disk is nearly full. Checked at most once a minute."""
        now = self.clock()
        if now - self._disk_checked < DISK_CHECK_SECONDS:
            return
        self._disk_checked = now
        problem = low_disk(self.store.path, self.min_free_gb, self.free)
        if problem:
            log.error("Stopping: %s", problem)
            self.stopped_for_disk = True
            self.md.stop()

    def on_tick(self, tick: Tick):
        self.check_disk()
        u = self.underlyings.get(tick.instrument)
        if u is None or tick.ltp is None:
            return
        last = self.centred_at.get(tick.instrument)
        if last is None or abs(tick.ltp - last) >= u["recenter_points"]:
            self.select(tick.instrument, u, tick.ltp)

    def select(self, index: InstrumentId, u: dict, spot: float):
        self.centred_at[index] = spot
        picked = pick_options(self.md.instruments(), u["exchange"], u["name"], spot,
                              u["strikes_each_side"], self.today)
        new = [c for c in picked if c.instrument not in self.subscribed]
        if not picked:
            log.warning("No contracts found for %s", index)
        if not new:
            return
        self.store.save_contracts(self.today.isoformat(), new)
        self.md.subscribe(c.instrument for c in new)
        self.subscribed.update(c.instrument for c in new)
        log.info("%s: +%d contracts (expiry %s, spot %.1f), total %d",
                 u["name"], len(new), new[0].instrument.expiry, spot, len(self.subscribed))

    def finish(self):
        self.writer.flush()
        log.info("Done. rows: ltp=%d depth=%d", self.store.count("ltp"), self.store.count("depth"))
        self.store.close()


def seconds_until(end: str) -> float:
    now = datetime.now(IST)
    end_dt = datetime.combine(now.date(), datetime.strptime(end, "%H:%M").time(), IST)
    return max(0.0, (end_dt - now).total_seconds())


def calendar_problems(cal: MarketCalendar, contracts: list[Contract], underlyings: list[dict]) -> list[str]:
    """Broker-listed expiries the calendar disagrees with (usually a wrong holiday list)."""
    problems = []
    for u in underlyings:
        index = InstrumentId.index(u["exchange"], u["name"])
        listed = {c.instrument.expiry for c in contracts
                  if c.instrument.exchange == u["exchange"] and c.instrument.underlying == u["name"]}
        try:
            problems += cal.check_expiries(index, listed)
        except UnknownYear as e:
            problems.append(str(e))
    return problems


def build(cfg: dict, api, today: date, **adapter_kw):
    """Wire the Groww adapter, store and recorder. Tests pass fakes via adapter_kw."""
    from tradeloop.brokers.groww import GrowwMarketData

    md = GrowwMarketData(api, depth=cfg.get("record_depth", True),
                         poll_seconds=float(cfg.get("poll_seconds", 0.5)),
                         stale_seconds=float(cfg.get("stale_seconds", 30)), **adapter_kw)
    return Recorder(cfg, md, Store(cfg["db_path"]), today), md


def paper_traders(cfg: dict, rec: Recorder, md: MarketData) -> list:
    """Paper strategies from cfg["paper"], listening to the recorder's ticks. A paper setup
    problem is logged and skipped: it must never stop the recording."""
    if not cfg.get("paper"):
        return []
    from tradeloop.lot_sizes import default_lot_sizes
    from tradeloop.paper import PaperTrader
    from tradeloop.paper_store import PaperStore

    listed = {c.instrument: c.lot_size for c in md.instruments()}
    table = default_lot_sizes()
    lot_size = lambda i: listed.get(i) or table.lot_size(i)       # broker's lot first
    store = PaperStore(cfg.get("paper_db", "data/paper.db"))
    traders = []
    for path in cfg["paper"]:
        try:
            t = PaperTrader(Path(path), store, max_lots=int(cfg.get("paper_max_lots", 1)), lot_size=lot_size,
                           kill_file=Path(cfg.get("kill_file", "data/KILL")))
        except Exception:
            log.exception("Paper strategy %s not started", path)
            continue
        rec.bus.subscribe(Topic.TICK, t.on_tick)
        traders.append(t)
    log.info("Paper trading: %s", ", ".join(t.strategy_id for t in traders) or "none")
    return traders


def main():
    from tradeloop.auth import login

    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    problem = low_disk(cfg["db_path"], float(cfg.get("min_free_gb", 5)))
    if problem:
        log.error("Not starting: %s", problem)
        sys.exit(1)

    cal = MarketCalendar.load()
    today = datetime.now(IST).date()
    try:
        if not cal.is_trading_day(today):
            log.info("Not a trading day (%s); nothing to record", cal.holiday(today) or "weekend")
            return
    except UnknownYear as e:
        log.warning("%s; recording anyway (a wasted holiday beats a missed trading day)", e)

    rec, md = build(cfg, login(), today)
    for problem in calendar_problems(cal, md.instruments(), cfg["underlyings"]):
        log.warning("Calendar check: %s", problem)
    paper_traders(cfg, rec, md)

    def handle(sig, _frame):
        log.info("Signal %s, stopping", sig)
        md.stop()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    timer = threading.Timer(seconds_until(cfg["end_time"]), md.stop)
    timer.daemon = True
    timer.start()

    rec.start()
    md.run()
    rec.finish()
    if rec.stopped_for_disk:
        sys.exit(1)


if __name__ == "__main__":
    main()
