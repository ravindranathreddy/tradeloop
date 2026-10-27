"""Execution for backtests: working orders fill against each minute's candle through the
FillModel. Implements the same `Execution` interface as Groww, so the engine can't tell
the difference (design rule 5).

The driver calls `advance(ts_ms, candles)` once per minute before the strategy runs;
orders placed during minute t become eligible from the next candle.
"""
from __future__ import annotations

from collections.abc import Callable

from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Order, OrderStatus, OrderUpdate, Position
from tradeloop.fill_model import FillModel, Working


class SimBroker:
    def __init__(self, fill_model: FillModel, lot_size: Callable[[InstrumentId], int]):
        self._model, self._lot_size = fill_model, lot_size
        self._working: dict[str, Working] = {}
        self._seen: set[str] = set()
        self._positions: dict[InstrumentId, int] = {}
        self._fill_handlers: list[Callable[[Fill], None]] = []
        self._update_handlers: list[Callable[[OrderUpdate], None]] = []
        self.now_ms = 0

    # --- Execution ---------------------------------------------------------------

    def place(self, order: Order) -> None:
        if order.client_ref in self._seen:                  # idempotent: a retry is not a new order
            return
        self._seen.add(order.client_ref)
        self._working[order.client_ref] = self._model.start(order, self.now_ms, self._lot_size(order.instrument))
        self._update(order.client_ref, OrderStatus.OPEN)

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

    # --- simulation ----------------------------------------------------------------

    def working(self) -> list[Order]:
        return [w.order for w in self._working.values()]

    def advance(self, ts_ms: int, candles: dict[InstrumentId, object]) -> None:
        """Match working orders against the candles that start at `ts_ms`."""
        self.now_ms = ts_ms
        for ref, w in list(self._working.items()):
            if self._model.expired(w, ts_ms):
                self.cancel(ref)
                continue
            row = candles.get(w.order.instrument)
            fill = self._model.step(w, row) if row is not None else None
            if fill is None:
                continue
            self._positions[fill.instrument] = self._positions.get(fill.instrument, 0) + fill.qty * fill.side.sign
            done = w.remaining == 0
            if done:
                del self._working[ref]
            for h in self._fill_handlers:
                h(fill)
            self._update(ref, OrderStatus.FILLED if done else OrderStatus.PARTIALLY_FILLED,
                         filled=w.order.qty - w.remaining)

    def end_of_minute(self, ts_ms: int) -> None:
        """Orders placed from here on are decided after the candle at ts_ms closed."""
        self.now_ms = ts_ms

    def _update(self, ref: str, status: OrderStatus, filled: int = 0) -> None:
        for h in self._update_handlers:
            h(OrderUpdate(ref, status, self.now_ms, filled_qty=filled))
