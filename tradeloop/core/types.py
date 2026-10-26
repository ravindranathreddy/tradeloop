"""Core data types (design rule 3). Adapters convert broker JSON into these at the boundary."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from tradeloop.core.ids import InstrumentId


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"          # stop-loss limit
    SL_M = "SL_M"      # stop-loss market


class Product(str, Enum):
    INTRADAY = "INTRADAY"   # squared off same day (MIS)
    CARRY = "CARRY"         # NRML / CNC


class OrderStatus(str, Enum):
    NEW = "NEW"                    # created locally, not yet accepted by risk
    REJECTED_RISK = "REJECTED_RISK"
    SENT = "SENT"
    OPEN = "OPEN"                  # accepted by broker, working
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"          # rejected by broker

    @property
    def is_final(self) -> bool:
        return self in (OrderStatus.REJECTED_RISK, OrderStatus.FILLED,
                        OrderStatus.CANCELLED, OrderStatus.REJECTED)


def new_client_ref() -> str:
    """Idempotency key (design rule 8): a retry reuses the same ref, never a new one."""
    return uuid.uuid4().hex[:20]


@dataclass(frozen=True, slots=True)
class Contract:
    """A tradable instrument and the facts needed to size orders in it."""
    instrument: InstrumentId
    lot_size: int


@dataclass(frozen=True, slots=True)
class Level:
    """One price level of the order book."""
    price: float
    qty: float


@dataclass(frozen=True, slots=True)
class Tick:
    """One market update. Fields a feed doesn't provide stay None (or empty).

    For an index, ltp is the index value. bids/asks are the book, best first;
    bid/ask/bid_qty/ask_qty repeat the best level for convenience.
    """
    instrument: InstrumentId
    ts_ms: int                     # exchange time
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    recv_ms: int | None = None     # when we received it (staleness, latency)
    volume: float | None = None    # traded today so far
    oi: float | None = None        # open interest (derivatives)
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()


@dataclass(frozen=True, slots=True)
class Order:
    instrument: InstrumentId
    side: Side
    qty: int
    order_type: OrderType
    product: Product
    strategy_id: str               # Rule #1: every order names the strategy that sent it
    config_version: str            # git hash of the strategy config (design rule 8)
    price: float | None = None     # LIMIT and SL
    trigger: float | None = None   # SL and SL_M
    client_ref: str = field(default_factory=new_client_ref)

    def __post_init__(self):
        if self.qty <= 0:
            raise ValueError("qty must be positive; direction is in side")
        if not self.strategy_id:
            raise ValueError("strategy_id is required (Rule #1)")
        if not self.config_version:
            raise ValueError("config_version is required")
        if self.order_type in (OrderType.LIMIT, OrderType.SL) and self.price is None:
            raise ValueError(f"{self.order_type.value} needs a price")
        if self.order_type in (OrderType.SL, OrderType.SL_M) and self.trigger is None:
            raise ValueError(f"{self.order_type.value} needs a trigger")


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """Asynchronous status change reported by a broker (design rule 7)."""
    client_ref: str
    status: OrderStatus
    ts_ms: int
    broker_order_id: str | None = None
    filled_qty: int = 0
    avg_price: float | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution. Charges are included so the ledger can record true P&L (rule 9)."""
    fill_id: str
    client_ref: str
    instrument: InstrumentId
    side: Side
    qty: int
    price: float
    ts_ms: int
    charges: float = 0.0


@dataclass(frozen=True, slots=True)
class Position:
    """Net position. qty is signed: positive long, negative short."""
    instrument: InstrumentId
    qty: int
    avg_price: float
