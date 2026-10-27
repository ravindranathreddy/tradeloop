"""The engine (design rule 5): one loop for backtest, paper and live. It hands the
strategy a view of the market each minute, sends the orders it asks for through an
`Execution`, and books fills in the ledger. Only the drivers differ: backtest replays
candles through SimBroker; paper and live will feed minutes from the recorder's feed.

Strategies see positions from confirmed fills only (design rule 7), never from orders
merely sent. Every order carries strategy_id and config_version (rule 8).

A strategy is any object with `on_minute(ctx: Context) -> None`; a new instance per
trading day, so state never leaks from one expiry to the next.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol

from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.interfaces import Execution
from tradeloop.core.market_calendar import IST
from tradeloop.core.types import Fill, Order, OrderType, Product, Side
from tradeloop.ledger import Ledger


@dataclass(frozen=True, slots=True)
class Quote:
    """Latest known minute for one contract. Greeks are None where they couldn't be derived."""
    ts_ms: int
    close: float
    volume: float | None
    oi: float | None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None


class Strategy(Protocol):
    def on_minute(self, ctx: Context) -> None: ...


class Context:
    """What a strategy sees and can do at the end of a minute."""

    def __init__(self, engine: Engine, now_ms: int, index: InstrumentId, expiry: date,
                 index_price: float, quotes: dict[InstrumentId, Quote]):
        self._engine = engine
        self.now_ms, self.index, self.expiry = now_ms, index, expiry
        self.index_price, self.quotes = index_price, quotes

    @property
    def time(self) -> time:
        """IST wall-clock time of this decision (the end of the minute just closed)."""
        return datetime.fromtimestamp(self.now_ms / 1000, IST).time()

    def positions(self) -> dict[InstrumentId, int]:
        """Signed quantities from confirmed fills."""
        return {p.instrument: p.qty for p in self._engine.ledger.positions()}

    def working_orders(self) -> list[Order]:
        return self._engine.working_orders()

    def options(self, kind: Kind | None = None) -> dict[InstrumentId, Quote]:
        """Quotes for options on this expiry (optionally only CE or PE)."""
        return {i: q for i, q in self.quotes.items()
                if i.kind.is_option and i.expiry == self.expiry and (kind is None or i.kind is kind)}

    def lot_size(self, instrument: InstrumentId) -> int:
        return self._engine.lot_size(instrument)

    def order(self, instrument: InstrumentId, side: Side, lots: int, order_type: OrderType = OrderType.MARKET,
              price: float | None = None, trigger: float | None = None) -> Order:
        """Build and send an order; quantity is lots x the contract's lot size."""
        order = Order(instrument, side, lots * self.lot_size(instrument), order_type, Product.INTRADAY,
                      self._engine.strategy_id, self._engine.config_version, price, trigger)
        self._engine.execution.place(order)
        return order

    def cancel(self, client_ref: str) -> None:
        self._engine.execution.cancel(client_ref)


class Engine:
    def __init__(self, strategy: Strategy, execution: Execution, ledger: Ledger, *, strategy_id: str,
                 config_version: str, lot_size: Callable[[InstrumentId], int],
                 working_orders: Callable[[], list[Order]] = list):
        self.strategy, self.execution, self.ledger = strategy, execution, ledger
        self.strategy_id, self.config_version = strategy_id, config_version
        self.lot_size, self.working_orders = lot_size, working_orders
        self.fills: list[Fill] = []
        execution.on_fill(self._on_fill)

    def _on_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        self.ledger.record_fill(fill)

    def on_minute(self, now_ms: int, index: InstrumentId, expiry: date, index_price: float,
                  quotes: dict[InstrumentId, Quote]) -> None:
        self.strategy.on_minute(Context(self, now_ms, index, expiry, index_price, quotes))
