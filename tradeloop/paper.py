"""Paper trading (step 7): approved-for-paper strategies run on the live feed, through the
same engine as the backtest, with fills against the live order book. No real orders.

Runs inside the recorder (config.yaml `paper:` lists strategy configs), listening to the
same ticks it records: one feed connection, and the paper trades can be checked later
against the recorded book. A failing paper trader is logged by the bus and never stops
the recording.

Each minute (on the first tick at least 2 s into a new minute, exchange time, so the order
books have refreshed) the trader hands the strategy the nearest expiry's option prices; orders go through the risk gate to the
paper broker. Fills, orders and strategy state go to data/paper.db, and the ledger is
rebuilt from it at start, so positions held overnight survive the nightly restart.

After the close on expiry days, settle at the official close, then report:

    python -m tradeloop.paper settle [--config config.yaml]
    python -m tradeloop.paper report [--config config.yaml]
"""
from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, time as dtime
from pathlib import Path

import yaml

from tradeloop.backtest import config_version
from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST, default_calendar
from tradeloop.core.types import Fill, Order, OrderUpdate, Position, Tick
from tradeloop.engine import Engine, Quote
from tradeloop.kill_switch import active as kill_active, flatten
from tradeloop.lot_sizes import default_lot_sizes
from tradeloop.paper_broker import PaperBroker
from tradeloop.paper_store import PaperStore
from tradeloop.risk import RiskGate
from tradeloop.strategies import REGISTRY

log = logging.getLogger("paper")
MINUTE_MS = 60_000
DECIDE_AFTER_MS = 2_000      # into the new minute, so every order book has refreshed


def _wall_ms() -> int:
    return int(time.time() * 1000)


def _day(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, IST).date()


class _Recorded:
    """Execution wrapper that writes every order to the paper store before sending it on."""

    def __init__(self, inner, store: PaperStore, clock_ms: Callable[[], int]):
        self.inner, self.store, self.clock = inner, store, clock_ms

    def place(self, order: Order) -> None:
        self.store.save_order(order, self.clock())
        self.inner.place(order)

    def cancel(self, client_ref: str) -> None:
        self.inner.cancel(client_ref)

    def cancel_all(self) -> None:
        self.inner.cancel_all()

    def positions(self) -> list[Position]:
        return self.inner.positions()

    def on_order_update(self, handler: Callable[[OrderUpdate], None]) -> None:
        self.inner.on_order_update(handler)

    def on_fill(self, handler: Callable[[Fill], None]) -> None:
        self.inner.on_fill(handler)


class PaperTrader:
    def __init__(self, cfg_path: Path, store: PaperStore, *, max_lots: int = 1,
                 lot_size: Callable[[InstrumentId], int] | None = None, clock_ms: Callable[[], int] = _wall_ms,
                 kill_file: Path | None = None):
        self.cfg = yaml.safe_load(Path(cfg_path).read_text())
        if len(self.cfg["underlyings"]) != 1:
            raise ValueError("paper trading runs one underlying per strategy config")
        self.strategy_id, self.version = self.cfg["strategy_id"], config_version(Path(cfg_path))
        self.index = InstrumentId.parse(self.cfg["underlyings"][0])
        self.store, self.clock = store, clock_ms
        self.lot_size = lot_size or default_lot_sizes().lot_size
        self.broker = PaperBroker(self.lot_size, clock_ms=clock_ms)
        gate = RiskGate(self.broker, max_lots=max_lots, lot_size=self.lot_size, fresh=self.broker.fresh,
                        clock_ms=clock_ms, halted=lambda: self.halted)
        self.gate = gate
        self.kill_file, self.halted = kill_file, False
        self._killed_ms: int | None = None         # the kill request already flattened
        self._kill_check_ms = 0
        execution = _Recorded(gate, store, clock_ms)
        self.ledger = store.rebuild_ledger(self.strategy_id)
        self.engine = Engine(None, execution, self.ledger, strategy_id=self.strategy_id, config_version=self.version,
                             lot_size=self.lot_size, working_orders=self.broker.working)
        self.broker.on_fill(lambda f: store.save_fill(self.strategy_id, f))
        gate.on_order_update(store.update_order)
        self.quotes: dict[InstrumentId, Quote] = {}
        self.index_price: float | None = None
        self.minute: int | None = None
        self.week: date | None = None
        held = self.ledger.positions()
        self.broker.load_positions({p.instrument: p.qty for p in held})
        log.info("%s (config %s): %d open positions from earlier days", self.strategy_id, self.version, len(held))

    def _check_kill(self) -> None:
        """At most once a second: halt on a kill request and flatten once per request. Orders go
        straight to the paper broker, past the gate (which is now closed) and the strategy."""
        now = self.clock()
        if self.kill_file is None or now - self._kill_check_ms < 1000:
            return
        self._kill_check_ms = now
        req = kill_active(self.kill_file)
        self.halted = req is not None
        if req and req["requested_ms"] != self._killed_ms:
            self._killed_ms = req["requested_ms"]
            if not req.get("flatten", True):
                log.warning("ALERT halted (%s): %s places no orders until resumed", req["reason"], self.strategy_id)
                return
            log.warning("ALERT kill switch (%s): flattening %s", req["reason"], self.strategy_id)
            flatten(_Recorded(self.broker, self.store, self.clock), self.ledger.positions(),
                    strategy_id=self.strategy_id, config_version=self.version, request_ms=req["requested_ms"])

    def on_tick(self, tick: Tick) -> None:
        self._check_kill()
        self.broker.on_tick(tick)
        iid = tick.instrument
        if iid == self.index and tick.ltp is not None:
            self.index_price = tick.ltp
        elif (tick.ltp is not None and iid.kind.is_option and iid.exchange == self.index.exchange
              and iid.underlying == self.index.underlying):
            self.quotes[iid] = Quote(tick.ts_ms, tick.ltp, tick.volume, tick.oi)
        m = tick.ts_ms // MINUTE_MS
        if self.minute is None:
            self.minute = m                     # start mid-minute: wait for the next one
        elif m > self.minute and tick.ts_ms - m * MINUTE_MS >= DECIDE_AFTER_MS:
            self.minute = m                     # the previous minute is complete
            self._on_minute(m * MINUTE_MS)

    def _on_minute(self, now_ms: int) -> None:
        today = _day(now_ms)
        expiries = [i.expiry for i in self.quotes if i.expiry >= today]
        if self.index_price is None or not expiries:
            return
        expiry = min(expiries)
        if expiry != self.week:
            self._start_week(expiry)
        self.broker.step()
        before = self.engine.strategy.state_dict()
        quotes = {i: q for i, q in self.quotes.items() if i.expiry == expiry}
        self.engine.on_minute(now_ms, self.index, expiry, self.index_price, quotes)
        after = self.engine.strategy.state_dict()
        if after != before:
            self.store.save_state(self.strategy_id, expiry, after, now_ms)
            log.info("%s: %s -> %s", self.strategy_id, before.get("state", before), after.get("state", after))

    def _start_week(self, expiry: date) -> None:
        strategy = REGISTRY[self.cfg["strategy"]](self.cfg)
        saved = self.store.load_state(self.strategy_id, expiry)
        if saved:
            strategy.restore(saved)
        self.engine.strategy, self.week = strategy, expiry
        log.info("%s: week of expiry %s, state %s", self.strategy_id, expiry, strategy.state_dict())


# --- after the close ------------------------------------------------------------------

def official_close(api, index: InstrumentId, day: date) -> float:
    """The index's official close (what expiring options settle on): the daily candle."""
    from tradeloop.brokers.groww import GrowwHistory

    start = datetime.combine(day, dtime(9, 0), IST)
    end = datetime.combine(day, default_calendar().close_time(day), IST)
    rows = [r for r in GrowwHistory(api).candles(index, start, end, interval="1day") if _day(r[1]) == day]
    if not rows:
        raise RuntimeError(f"no official close for {index} on {day} yet; run settle again later")
    return float(rows[-1][5])


def settle(cfg: dict, store: PaperStore, api, day: date) -> list[str]:
    """Settle every paper position expiring on `day` at the official close."""
    out = []
    for path in cfg.get("paper", []):
        scfg = yaml.safe_load(Path(path).read_text())
        sid, index = scfg["strategy_id"], InstrumentId.parse(scfg["underlyings"][0])
        ledger = store.rebuild_ledger(sid)
        expiring = [p for p in ledger.positions() if p.instrument.expiry == day]
        if not expiring:
            out.append(f"{sid}: nothing expiring on {day}")
            continue
        close = official_close(api, index, day)
        ts = int(datetime.combine(day, default_calendar().close_time(day), IST).timestamp() * 1000)
        store.save_settlement(sid, day, index.underlying, close, ts)
        out.append(f"{sid}: settled {len(expiring)} positions at the official close {close:.2f}")
    return out


def report(cfg: dict, store: PaperStore) -> str:
    lines = []
    for path in cfg.get("paper", []):
        scfg = yaml.safe_load(Path(path).read_text())
        sid = scfg["strategy_id"]
        ledger = store.rebuild_ledger(sid)
        weeks = store.query("SELECT expiry FROM settlements WHERE strategy_id = ? ORDER BY expiry", (sid,))
        orders = store.query("SELECT status, COUNT(*) FROM orders WHERE strategy_id = ? GROUP BY status", (sid,))
        lines.append(f"{sid} (config {config_version(Path(path))})")
        lines.append(f"  settled weeks {len(weeks)}; realised gross Rs {ledger.realised_pnl() / 100:,.2f}, "
                     f"charges Rs {ledger.total_charges() / 100:,.2f}, net Rs {ledger.net_pnl() / 100:,.2f}")
        lines.append("  orders: " + (", ".join(f"{s} {n}" for s, n in orders) or "none"))
        for p in ledger.positions():
            lines.append(f"  open: {p.qty:+d} {p.instrument} @ {p.avg_price:.2f}")
        for fid, inst, side, qty, price, ts in store.query(
                "SELECT fill_id, instrument, side, qty, price, ts_ms FROM fills WHERE strategy_id = ? "
                "ORDER BY ts_ms DESC LIMIT 6", (sid,)):
            lines.append(f"  fill {datetime.fromtimestamp(ts / 1000, IST):%Y-%m-%d %H:%M:%S} {side} {qty} {inst} @ {price}")
    return "\n".join(lines) or "No paper strategies in the config"


def main():
    from tradeloop.auth import login

    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["settle", "report"])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--day", default=datetime.now(IST).date().isoformat(), help="expiry day to settle")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(Path(a.config).read_text())
    store = PaperStore(cfg.get("paper_db", "data/paper.db"))
    if a.command == "settle":
        for line in settle(cfg, store, login(), date.fromisoformat(a.day)):
            print(line)
    print(report(cfg, store))


if __name__ == "__main__":
    main()
