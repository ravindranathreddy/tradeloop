"""Groww order-side translation (stage 1 of the Execution adapter): pure functions, no network,
no SDK import. Neutral types in, Groww's request fields out, and the reverse.

Everything here follows Groww's Python SDK docs (orders, positions, margin, annexures) as read
on 31 Oct 2026. Where the docs are silent the code fails loudly instead of guessing:

  - an unknown order status raises UnknownStatus (a new status must not be silently misread);
  - a position whose `quantity` disagrees with its credit/debit/carry-forward fields raises
    PositionAmbiguous (the docs don't say how they combine; the first read-only look at the real
    account settles it);
  - a position in a contract we can't name raises UnknownSymbol (treat as untagged: Rule #1).

Idempotency: Groww's `order_reference_id` is 8-20 letters/digits with at most two hyphens, and the
docs don't say duplicates are refused. So references are deterministic (same client_ref, same
reference) and the adapter must look an order up by reference before any retry.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
import pandas as pd

from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Order, OrderStatus, OrderType, OrderUpdate, Position, Product, Side

SEGMENT = "FNO"
REF_PREFIX = "tl"            # tradeloop: an order without it was not placed by the bot (Rule #1)
_REF = re.compile(rf"{REF_PREFIX}[0-9a-f]{{18}}")


class UnknownStatus(ValueError):
    pass


class PositionAmbiguous(ValueError):
    pass


class UnknownSymbol(KeyError):
    pass


class OrderRejected(ValueError):
    """An order that fails our own sanity checks; never sent."""


class InconsistentFill(ValueError):
    pass


# --- instruments -------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Symbol:
    """What the order API needs to know about a contract (from Groww's instrument CSV)."""
    trading_symbol: str
    exchange: str
    lot_size: int
    tick_size: float
    freeze_quantity: int          # largest quantity the exchange accepts in one order
    buy_allowed: bool
    sell_allowed: bool


def symbols_from_rows(df: pd.DataFrame) -> dict[InstrumentId, Symbol]:
    """Option rows of the instrument CSV -> {neutral ID: Symbol}. Bad rows are skipped."""
    opts = df[(df["segment"] == SEGMENT) & df["instrument_type"].isin(["CE", "PE"])]
    out = {}
    for r in opts.itertuples(index=False):
        try:
            expiry = pd.to_datetime(r.expiry_date, errors="coerce")
            if pd.isna(expiry):
                continue
            iid = InstrumentId.option(r.exchange, r.underlying_symbol, expiry.date(), float(r.strike_price), r.instrument_type)
            out[iid] = Symbol(str(r.trading_symbol), r.exchange, int(r.lot_size), float(r.tick_size),
                              int(r.freeze_quantity), _flag(r.buy_allowed), _flag(r.sell_allowed))
        except (TypeError, ValueError):
            continue
    return out


def _flag(v) -> bool:
    return str(v).strip().upper() in ("1", "TRUE", "Y", "YES", "1.0")


def by_trading_symbol(symbols: dict[InstrumentId, Symbol]) -> dict[str, InstrumentId]:
    return {s.trading_symbol: i for i, s in symbols.items()}


# --- references ---------------------------------------------------------------------------------

def reference_id(client_ref: str) -> str:
    """Groww's order_reference_id for our client_ref: 'tl' + 18 hex characters (20, no hyphens).
    Deterministic, so a retry of the same order carries the same reference."""
    return REF_PREFIX + hashlib.sha1(client_ref.encode()).hexdigest()[:18]


def is_ours(reference: str | None) -> bool:
    return bool(reference and _REF.fullmatch(reference))


# --- orders out ---------------------------------------------------------------------------------

_PRODUCT = {Product.CARRY: "NRML", Product.INTRADAY: "MIS"}
_TYPE = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT", OrderType.SL: "SL", OrderType.SL_M: "SL_M"}


def check_order(order: Order, sym: Symbol) -> None:
    """Our own sanity checks before anything is sent (design rule 11). Raises OrderRejected."""
    if order.qty % sym.lot_size:
        raise OrderRejected(f"{order.qty} is not a whole number of lots of {sym.lot_size}")
    if order.qty > sym.freeze_quantity:
        raise OrderRejected(f"{order.qty} exceeds the exchange freeze quantity {sym.freeze_quantity}")
    if order.side is Side.BUY and not sym.buy_allowed or order.side is Side.SELL and not sym.sell_allowed:
        raise OrderRejected(f"{order.side.value} is not allowed on {sym.trading_symbol}")
    for name, px in (("price", order.price), ("trigger", order.trigger)):
        if px is not None and (px <= 0 or abs(px / sym.tick_size - round(px / sym.tick_size)) > 1e-6):
            raise OrderRejected(f"{name} {px} is not a positive multiple of the tick {sym.tick_size}")


def place_params(order: Order, sym: Symbol) -> dict:
    """Keyword arguments for GrowwAPI.place_order. Runs the sanity checks first."""
    check_order(order, sym)
    p = {"trading_symbol": sym.trading_symbol, "quantity": order.qty, "validity": "DAY", "exchange": sym.exchange,
         "segment": SEGMENT, "product": _PRODUCT[order.product], "order_type": _TYPE[order.order_type],
         "transaction_type": order.side.value, "order_reference_id": reference_id(order.client_ref)}
    if order.order_type in (OrderType.LIMIT, OrderType.SL):
        p["price"] = order.price
    if order.order_type in (OrderType.SL, OrderType.SL_M):
        p["trigger_price"] = order.trigger
    return p


# --- order status in ----------------------------------------------------------------------------

_WORKING = {"NEW", "ACKED", "APPROVED", "TRIGGER_PENDING", "MODIFICATION_REQUESTED", "CANCELLATION_REQUESTED"}
_DONE = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"}


def order_status(status: str, filled: int, qty: int) -> OrderStatus:
    """Groww order_status + filled quantity -> our OrderStatus. Groww has no partial-fill status:
    a working order with some quantity filled is PARTIALLY_FILLED."""
    s = str(status).upper()
    if s in _WORKING:
        return OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.OPEN
    if s in _DONE:
        if filled >= qty:
            return OrderStatus.FILLED
        if filled > 0:
            return OrderStatus.PARTIALLY_FILLED
        raise UnknownStatus(f"{s} with nothing filled")
    if s == "CANCELLED":
        return OrderStatus.CANCELLED
    if s in ("REJECTED", "FAILED"):
        return OrderStatus.REJECTED
    raise UnknownStatus(f"unrecognised Groww order status {status!r}")


def _pick(raw: dict, *names, default=None):
    for n in names:
        if raw.get(n) is not None:
            return raw[n]
    return default


def order_update(raw: dict, client_ref: str, ts_ms: int, qty: int | None = None) -> OrderUpdate:
    """From a REST order status/detail or a live order-update payload (the two use different
    field names: order_status / orderStatus, filled_quantity / filledQty, ...)."""
    qty = int(_pick(raw, "quantity", "qty", default=qty or 0))
    filled = int(float(_pick(raw, "filled_quantity", "filledQty", default=0)))
    avg = _pick(raw, "average_fill_price", "avgFillPrice")
    return OrderUpdate(client_ref, order_status(_pick(raw, "order_status", "orderStatus", default=""), filled, qty),
                       ts_ms, broker_order_id=_pick(raw, "groww_order_id", "growwOrderId"), filled_qty=filled,
                       avg_price=float(avg) if avg else None, message=_pick(raw, "remark", "message"))


class FillTracker:
    """Turns cumulative (filled quantity, average price) reports into one Fill per increase.

    Groww reports no separate fills: only how much of the order has filled so far and at what
    average. The price of each new slice is whatever keeps the running average right. fill_id is
    '<client_ref>-<cumulative quantity>', so seeing the same report twice emits nothing."""

    def __init__(self):
        self._seen: dict[str, tuple[int, float]] = {}        # client_ref -> (quantity, total value)

    def seed(self, client_ref: str, qty: int, avg_price: float) -> None:
        """Fills already booked (after a restart), so they are not emitted again."""
        self._seen[client_ref] = (qty, qty * avg_price)

    def update(self, client_ref: str, instrument: InstrumentId, side: Side, cum_qty: int,
               avg_price: float | None, ts_ms: int) -> Fill | None:
        old_qty, old_value = self._seen.get(client_ref, (0, 0.0))
        if cum_qty < old_qty:
            raise InconsistentFill(f"{client_ref}: filled quantity fell from {old_qty} to {cum_qty}")
        if cum_qty == old_qty:
            return None
        if not avg_price:
            raise InconsistentFill(f"{client_ref}: {cum_qty} filled but no average price")
        value = cum_qty * avg_price
        price = round((value - old_value) / (cum_qty - old_qty), 4)
        self._seen[client_ref] = (cum_qty, value)
        return Fill(f"{client_ref}-{cum_qty}", client_ref, instrument, side, cum_qty - old_qty, price, ts_ms)


# --- positions in -------------------------------------------------------------------------------

def net_quantity(raw: dict) -> int:
    """Signed net quantity of a get_positions_for_user row. Two readings must agree: the
    `quantity` field, and credits minus debits (today's plus carried forward)."""
    stated = int(float(raw.get("quantity") or 0))
    f = lambda k: float(raw.get(k) or 0)
    computed = int((f("credit_quantity") - f("debit_quantity")) + (f("carry_forward_credit_quantity") - f("carry_forward_debit_quantity")))
    if stated != computed:
        raise PositionAmbiguous(f"{raw.get('trading_symbol')}: quantity {stated} but credits/debits give {computed}; "
                                "check how Groww reports carried-forward positions")
    return stated


def position(raw: dict, symbols: dict[str, InstrumentId]) -> Position | None:
    """One positions row -> Position, or None if flat. UnknownSymbol for a contract we can't name."""
    qty = net_quantity(raw)
    if qty == 0:
        return None
    sym = raw.get("trading_symbol")
    if sym not in symbols:
        raise UnknownSymbol(f"position in {sym!r}, which is not an option we know: untagged (Rule #1)")
    price = float(raw.get("net_price") or raw.get("net_carry_forward_price") or 0)
    return Position(symbols[sym], qty, price)


def positions(rows: Iterable[dict], symbols: dict[str, InstrumentId]) -> list[Position]:
    return [p for p in (position(r, symbols) for r in rows) if p]


# --- margin -------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MarginRequirement:
    """Rupees, as returned by get_order_margin_details."""
    total: float
    span: float
    exposure: float
    option_premium: float
    charges: float


@dataclass(frozen=True, slots=True)
class MarginAvailable:
    clear_cash: float
    option_sell: float            # option_sell_balance_available
    option_buy: float
    net_used: float


def margin_request(orders: list[tuple[Order, Symbol]]) -> dict:
    """Keyword arguments for GrowwAPI.get_order_margin_details: all legs of a spread in one
    basket, so Groww can apply its hedge benefit."""
    legs = []
    for o, s in orders:
        leg = {"trading_symbol": s.trading_symbol, "quantity": o.qty, "exchange": s.exchange,
               "product": _PRODUCT[o.product], "order_type": _TYPE[o.order_type], "transaction_type": o.side.value}
        if o.order_type is OrderType.LIMIT:
            leg["price"] = o.price
        legs.append(leg)
    return {"segment": SEGMENT, "orders": legs}


def parse_margin(resp: dict) -> MarginRequirement:
    g = lambda k: float(resp.get(k) or 0)
    return MarginRequirement(g("total_requirement"), g("span_required"), g("exposure_required"),
                             g("option_buy_premium"), g("brokerage_and_charges"))


def parse_available(resp: dict) -> MarginAvailable:
    fno = resp.get("fno_margin_details") or {}
    return MarginAvailable(float(resp.get("clear_cash") or 0), float(fno.get("option_sell_balance_available") or 0),
                           float(fno.get("option_buy_balance_available") or 0), float(resp.get("net_margin_used") or 0))


def margin_ok(required: MarginRequirement, available: MarginAvailable, buffer_pct: float = 10.0) -> bool:
    """Enough option-selling balance for the basket plus a buffer (design rule 11)."""
    return available.option_sell >= required.total * (1 + buffer_pct / 100)


# --- errors -------------------------------------------------------------------------------------

def classify_error(exc: BaseException) -> str:
    """What to do after a failed call, by exception type name (so no SDK import is needed):

      unknown   the order may or may not exist (timeouts, lost connections, anything unrecognised):
                look it up by reference before any retry; never send it again blindly
      retry     rate limited: nothing was accepted, wait and send again
      rejected  Groww refused the request outright
      auth      credentials or permissions: stop and alert
    """
    name = type(exc).__name__
    if name == "GrowwAPIRateLimitException":
        return "retry"
    if name in ("GrowwAPIBadRequestException", "GrowwAPINotFoundException"):
        return "rejected"
    if name in ("GrowwAPIAuthenticationException", "GrowwAPIAuthorisationException"):
        return "auth"
    return "unknown"

