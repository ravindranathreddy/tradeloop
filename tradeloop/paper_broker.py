"""Paper broker (step 7): an `Execution` that fills against the live order book.

Market orders take the book as it stands: buys from the asks, sells into the bids, best
level first and deeper levels for the rest, in whole lots. Liquidity used is remembered
until the next book update, so two orders can't both take the same quantity. A limit
order takes only levels at or better than its price. Nothing fills from a book older
than `stale_ms` (wall clock, by receive time); the order waits for a fresh one, and
gives up after `max_wait_minutes`. Stop orders aren't supported yet.

Fills are reported through on_fill like a real broker's, one per book level; the ledger
adds charges. No real order is ever sent.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Order, OrderStatus, OrderType, OrderUpdate, Position, Side, Tick

MINUTE_MS = 60_000


@dataclass(slots=True)
class _Working:
    order: Order
    placed_ms: int
    remaining: int
    fills: int = 0


@dataclass(slots=True)
class _Book:
    recv_ms: int
    bids: list[list[float]] = field(default_factory=list)     # [price, qty left], best first
    asks: list[list[float]] = field(default_factory=list)


class PaperBroker:
    def __init__(self, lot_size: Callable[[InstrumentId], int], *, stale_ms: int = 10_000,
                 max_wait_minutes: int = 5, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._lot_size, self._stale_ms, self._wait_ms = lot_size, stale_ms, max_wait_minutes * MINUTE_MS
        self._clock = clock_ms
        self._books: dict[InstrumentId, _Book] = {}
        self._working: dict[str, _Working] = {}
        self._seen: set[str] = set()
        self._positions: dict[InstrumentId, int] = {}
        self._fill_handlers: list[Callable[[Fill], None]] = []
        self._update_handlers: list[Callable[[OrderUpdate], None]] = []

    # --- market data ---------------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        if not (tick.bids or tick.asks):
            return
        self._books[tick.instrument] = _Book(tick.recv_ms or self._clock(),
                                             [[lv.price, lv.qty] for lv in tick.bids],
                                             [[lv.price, lv.qty] for lv in tick.asks])
        for ref in [r for r, w in self._working.items() if w.order.instrument == tick.instrument]:
            self._try(ref)

    def fresh(self, instrument: InstrumentId) -> bool:
        book = self._books.get(instrument)
        return book is not None and self._clock() - book.recv_ms <= self._stale_ms

    # --- Execution -----------------------------------------------------------------

    def place(self, order: Order) -> None:
        if order.client_ref in self._seen:                  # idempotent: a retry is not a new order
            return
        self._seen.add(order.client_ref)
        if order.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            self._update(order.client_ref, OrderStatus.REJECTED, message="paper broker: market and limit only")
            return
        if order.qty % self._lot_size(order.instrument):
            self._update(order.client_ref, OrderStatus.REJECTED, message="not a whole number of lots")
            return
        self._working[order.client_ref] = _Working(order, self._clock(), order.qty)
        self._update(order.client_ref, OrderStatus.OPEN)
        self._try(order.client_ref)

    def cancel(self, client_ref: str) -> None:
        if self._working.pop(client_ref, None) is not None:
            self._update(client_ref, OrderStatus.CANCELLED)

    def cancel_all(self) -> None:
        for ref in list(self._working):
            self.cancel(ref)

    def positions(self) -> list[Position]:
        return [Position(i, q, 0.0) for i, q in sorted(self._positions.items(), key=lambda kv: str(kv[0])) if q]

    def on_order_update(self, handler: Callable[[OrderUpdate], None]) -> None:
        self._update_handlers.append(handler)

    def on_fill(self, handler: Callable[[Fill], None]) -> None:
        self._fill_handlers.append(handler)

    def working(self) -> list[Order]:
        return [w.order for w in self._working.values()]

    def step(self) -> None:
        """Retry working orders and drop the ones past their wait. Call about once a minute."""
        now = self._clock()
        for ref, w in list(self._working.items()):
            if now - w.placed_ms >= self._wait_ms:
                self.cancel(ref)
            else:
                self._try(ref)

    # --- matching --------------------------------------------------------------------

    def _try(self, ref: str) -> None:
        w = self._working.get(ref)
        if w is None or not self.fresh(w.order.instrument):
            return
        book = self._books[w.order.instrument]
        buy = w.order.side is Side.BUY
        lot = self._lot_size(w.order.instrument)
        for level in book.asks if buy else book.bids:
            if w.remaining == 0:
                break
            price, available = level
            if w.order.order_type is OrderType.LIMIT and (price > w.order.price if buy else price < w.order.price):
                break
            qty = min(w.remaining, int(available) // lot * lot)
            if qty <= 0:
                continue
            level[1] -= qty
            w.remaining -= qty
            w.fills += 1
            fill = Fill(f"{ref}-{w.fills}", ref, w.order.instrument, w.order.side, qty, price, self._clock())
            self._positions[fill.instrument] = self._positions.get(fill.instrument, 0) + qty * fill.side.sign
            for h in self._fill_handlers:
                h(fill)
        if w.fills:
            done = w.remaining == 0
            if done:
                del self._working[ref]
            self._update(ref, OrderStatus.FILLED if done else OrderStatus.PARTIALLY_FILLED,
                         filled=w.order.qty - w.remaining)

    def _update(self, ref: str, status: OrderStatus, filled: int = 0, message: str | None = None) -> None:
        for h in self._update_handlers:
            h(OrderUpdate(ref, status, self._clock(), filled_qty=filled, message=message))
