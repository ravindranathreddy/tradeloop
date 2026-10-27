"""Risk gate (design rule 11), the basic version paper trading needs: every order passes
through here before reaching a broker.

Checks, in order:
  max lots    no single order larger than `max_lots`
  fresh data  no order without a fresh order book for the instrument (rule 11: no new
              entries on stale ticks); the strategy sees a rejection, never a silent drop

Rejections are reported as REJECTED_RISK order updates and logged as alerts.
Capital buckets, margin and daily loss limits come with live trading.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from tradeloop.core.ids import InstrumentId
from tradeloop.core.interfaces import Execution
from tradeloop.core.types import Fill, Order, OrderStatus, OrderUpdate, Position

log = logging.getLogger("risk")


class RiskGate:
    def __init__(self, inner: Execution, *, max_lots: int, lot_size: Callable[[InstrumentId], int],
                 fresh: Callable[[InstrumentId], bool], clock_ms: Callable[[], int]):
        self.inner, self.max_lots, self.lot_size, self.fresh, self.clock = inner, max_lots, lot_size, fresh, clock_ms
        self._update_handlers: list[Callable[[OrderUpdate], None]] = []
        self.rejected: list[tuple[Order, str]] = []
        inner.on_order_update(lambda u: [h(u) for h in self._update_handlers])

    def place(self, order: Order) -> None:
        reason = self._check(order)
        if reason:
            self.rejected.append((order, reason))
            log.warning("ALERT risk rejected %s %d %s: %s", order.side.value, order.qty, order.instrument, reason)
            for h in self._update_handlers:
                h(OrderUpdate(order.client_ref, OrderStatus.REJECTED_RISK, self.clock(), message=reason))
            return
        self.inner.place(order)

    def _check(self, order: Order) -> str | None:
        lots = order.qty // self.lot_size(order.instrument)
        if lots > self.max_lots:
            return f"{lots} lots exceeds the limit of {self.max_lots}"
        if not self.fresh(order.instrument):
            return "no fresh order book (stale data)"
        return None

    def cancel(self, client_ref: str) -> None:
        self.inner.cancel(client_ref)

    def cancel_all(self) -> None:
        self.inner.cancel_all()

    def positions(self) -> list[Position]:
        return self.inner.positions()

    def on_order_update(self, handler: Callable[[OrderUpdate], None]) -> None:
        self._update_handlers.append(handler)

    def on_fill(self, handler: Callable[[Fill], None]) -> None:
        self.inner.on_fill(handler)
