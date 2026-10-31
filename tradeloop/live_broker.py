"""The live broker: GrowwExecution behind the same surface the paper trader already uses.

PaperTrader drives a broker through on_tick / step / fresh / working / load_positions plus the
Execution methods. LiveBroker supplies those on top of GrowwExecution, so the strategy, risk gate,
kill switch, watchdog and ledger run unchanged and only the broker underneath differs.

Two things differ from paper, both because Groww answers on its own clock:
  fills       arrive by asking Groww. While an order is working, a tick polls at most once a second
              (and step() polls too); a fill is booked on the poll that sees it.
  prices      a MARKET order is sent as a marketable LIMIT: best ask plus a few ticks to buy, best
              bid minus a few to sell, from the last order book. It fills like a market order but
              can't run through a thin book. With no book at all the order goes out as MARKET.
              A book older than `stale_ms` still prices the order (the kill switch must be able to
              exit when the feed is down) but with a wide buffer.

Nothing here arms the adapter: `armed` stays whatever GrowwExecution was built with.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from tradeloop.brokers.groww_execution import GrowwExecution
from tradeloop.brokers.groww_orders import Symbol
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Order, OrderType, OrderUpdate, Position, Side, Tick

log = logging.getLogger("live")
MINUTE_MS = 60_000


@dataclass(slots=True)
class _Book:
    recv_ms: int
    bid: float | None
    ask: float | None


class LiveBroker:
    def __init__(self, execution: GrowwExecution, symbols: dict[InstrumentId, Symbol], *, stale_ms: int = 10_000,
                 max_wait_minutes: float = 2, buffer_ticks: int = 4, stale_buffer_pct: float = 10,
                 poll_ms: int = 1000, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.execution, self._symbols = execution, symbols
        self._stale_ms, self._wait_ms = stale_ms, int(max_wait_minutes * MINUTE_MS)
        self._buffer_ticks, self._stale_pct, self._poll_ms = buffer_ticks, stale_buffer_pct, poll_ms
        self._clock = clock_ms
        self._books: dict[InstrumentId, _Book] = {}
        self._placed_ms: dict[str, int] = {}
        self._last_poll_ms = 0

    @property
    def armed(self) -> bool:
        return self.execution.armed

    # --- market data and the clock -------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        if tick.bids or tick.asks:
            self._books[tick.instrument] = _Book(tick.recv_ms or self._clock(),
                                                 tick.bids[0].price if tick.bids else None,
                                                 tick.asks[0].price if tick.asks else None)
        if self.execution.working():
            self._poll(self._clock())

    def fresh(self, instrument: InstrumentId) -> bool:
        book = self._books.get(instrument)
        return book is not None and self._clock() - book.recv_ms <= self._stale_ms

    def step(self) -> None:
        """About once a minute: poll, and cancel what has waited too long (the price ran away)."""
        now = self._clock()
        self._poll(now, force=True)
        for o in self.execution.working():
            if now - self._placed_ms.get(o.client_ref, now) >= self._wait_ms:
                log.warning("%s %d %s unfilled after %ds: cancelling", o.side.value, o.qty, o.instrument,
                            self._wait_ms // 1000)
                self.execution.cancel(o.client_ref)

    def _poll(self, now: int, force: bool = False) -> None:
        if force or now - self._last_poll_ms >= self._poll_ms:
            self._last_poll_ms = now
            self.execution.poll()

    # --- Execution -----------------------------------------------------------------------

    def place(self, order: Order) -> None:
        self._placed_ms.setdefault(order.client_ref, self._clock())
        self.execution.place(self._priced(order))

    def _priced(self, order: Order) -> Order:
        sym, book = self._symbols.get(order.instrument), self._books.get(order.instrument)
        if order.order_type is not OrderType.MARKET or sym is None:
            return order
        ref = None if book is None else (book.ask if order.side is Side.BUY else book.bid)
        if not ref:
            log.warning("no order book for %s: sending %s as MARKET", order.instrument, order.client_ref)
            return order
        stale = self._clock() - book.recv_ms > self._stale_ms
        tick = sym.tick_size
        buffer = max(ref * self._stale_pct / 100, 10 * tick) if stale else self._buffer_ticks * tick
        if order.side is Side.BUY:
            price = math.ceil(round((ref + buffer) / tick, 6)) * tick
        else:
            price = max(math.floor(round((ref - buffer) / tick, 6)) * tick, tick)
        price = round(price, 2)
        log.info("%s %d %s: MARKET sent as LIMIT %.2f (%s %.2f%s)", order.side.value, order.qty, order.instrument,
                 price, "ask" if order.side is Side.BUY else "bid", ref, ", STALE book" if stale else "")
        return replace(order, order_type=OrderType.LIMIT, price=price)

    def cancel(self, client_ref: str) -> None:
        self.execution.cancel(client_ref)

    def cancel_all(self) -> None:
        self.execution.cancel_all()

    def positions(self) -> list[Position]:
        return self.execution.positions()

    def load_positions(self, positions: dict[InstrumentId, int]) -> None:
        """Nothing to load: Groww holds the positions. Startup reconciliation compares them with the ledger."""

    def on_order_update(self, handler: Callable[[OrderUpdate], None]) -> None:
        self.execution.on_order_update(handler)

    def on_fill(self, handler: Callable[[Fill], None]) -> None:
        self.execution.on_fill(handler)

    def working(self) -> list[Order]:
        return self.execution.working()
