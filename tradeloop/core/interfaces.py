"""The two broker interfaces (design rules 1 and 5).

Live brokers implement both. The paper broker implements Execution; the replay
feed implements MarketData. The engine only ever sees these interfaces, so live,
paper and backtest run the same strategy code.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Order, OrderUpdate, Position, Tick


@runtime_checkable
class MarketData(Protocol):
    def instruments(self) -> Iterable[InstrumentId]:
        """Tradable instruments right now (used to pick strikes, expiries)."""

    def subscribe(self, ids: Iterable[InstrumentId]) -> None:
        """Start streaming these instruments. Adding more later is allowed."""

    def on_tick(self, handler: Callable[[Tick], None]) -> None:
        """Register a handler; the feed calls it for every update."""

    def run(self) -> None:
        """Pump updates until stopped (blocking). Replay returns at end of data."""

    def stop(self) -> None: ...


@runtime_checkable
class Execution(Protocol):
    def place(self, order: Order) -> None:
        """Send an order. Idempotent on order.client_ref: a repeat must not duplicate."""

    def cancel(self, client_ref: str) -> None: ...

    def cancel_all(self) -> None:
        """Used by the kill switch."""

    def positions(self) -> list[Position]:
        """The broker's view of net positions (for reconciliation)."""

    def on_order_update(self, handler: Callable[[OrderUpdate], None]) -> None: ...

    def on_fill(self, handler: Callable[[Fill], None]) -> None: ...
