"""Groww order-side translation, offline. Payload shapes follow the Groww Python SDK docs."""
from datetime import date

import pandas as pd
import pytest

from tradeloop.brokers import groww_orders as g
from tradeloop.brokers.groww import contracts_from_rows
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Order, OrderStatus, OrderType, Product, Side

EXP = date(2026, 11, 3)
SHORT = InstrumentId.option("NSE", "NIFTY", EXP, 24400, "PE")
LONG = InstrumentId.option("NSE", "NIFTY", EXP, 23900, "PE")
ROWS = pd.DataFrame([
    {"exchange": "NSE", "segment": "FNO", "exchange_token": "41743", "trading_symbol": "NIFTY26N0324400PE",
     "instrument_type": "PE", "underlying_symbol": "NIFTY", "expiry_date": "2026-11-03", "strike_price": 24400.0,
     "lot_size": 65.0, "tick_size": 0.05, "freeze_quantity": 1755.0, "buy_allowed": 1, "sell_allowed": 1},
    {"exchange": "NSE", "segment": "FNO", "exchange_token": "41744", "trading_symbol": "NIFTY26N0323900PE",
     "instrument_type": "PE", "underlying_symbol": "NIFTY", "expiry_date": "2026-11-03", "strike_price": 23900.0,
     "lot_size": 65.0, "tick_size": 0.05, "freeze_quantity": 1755.0, "buy_allowed": 1, "sell_allowed": 0},
    {"exchange": "NSE", "segment": "FNO", "exchange_token": "9", "trading_symbol": "BAD", "instrument_type": "CE",
     "underlying_symbol": "NIFTY", "expiry_date": "garbage", "strike_price": 1.0, "lot_size": 65.0, "tick_size": 0.05,
     "freeze_quantity": 1755.0, "buy_allowed": 1, "sell_allowed": 1},
    {"exchange": "NSE", "segment": "CASH", "exchange_token": "2885", "trading_symbol": "RELIANCE",
     "instrument_type": "EQ", "underlying_symbol": None, "expiry_date": None, "strike_price": None, "lot_size": 1.0,
     "tick_size": 0.05, "freeze_quantity": 0.0, "buy_allowed": 1, "sell_allowed": 1},
])
SYMS = g.symbols_from_rows(ROWS)
BY_NAME = g.by_trading_symbol(SYMS)


def order(inst=SHORT, side=Side.SELL, qty=65, otype=OrderType.MARKET, price=None, trigger=None, ref="abc"):
    return Order(inst, side, qty, otype, Product.CARRY, "pcs", "v1", price, trigger, client_ref=ref)


# --- instruments --------------------------------------------------------------------------------

def test_symbols_use_the_same_neutral_ids_as_the_feed_adapter_and_skip_bad_rows():
    assert set(SYMS) == {SHORT, LONG}                                   # equity and the bad row are out
    assert set(SYMS) == {c.instrument for c in contracts_from_rows(ROWS).values()}
    s = SYMS[SHORT]
    assert (s.trading_symbol, s.lot_size, s.tick_size, s.freeze_quantity) == ("NIFTY26N0324400PE", 65, 0.05, 1755)
    assert SYMS[LONG].sell_allowed is False
    assert BY_NAME["NIFTY26N0324400PE"] == SHORT


# --- references ---------------------------------------------------------------------------------

def test_references_fit_groww_rules_are_stable_and_recognisable():
    for ref in ("0123456789abcdef0123", "kill-1793592938210-put_credit_spread-NSE|NIFTY|2026-11-03|24400|PE-1"):
        r = g.reference_id(ref)
        assert len(r) == 20 and r.isalnum() and r == g.reference_id(ref) and g.is_ours(r)
    assert g.reference_id("a") != g.reference_id("b")
    assert not g.is_ours(None) and not g.is_ours("") and not g.is_ours("MANUAL12345") and not g.is_ours("tl" + "g" * 18)


# --- orders out ---------------------------------------------------------------------------------

def test_market_order_params():
    p = g.place_params(order(ref="r1"), SYMS[SHORT])
    assert p == {"trading_symbol": "NIFTY26N0324400PE", "quantity": 65, "validity": "DAY", "exchange": "NSE",
                 "segment": "FNO", "product": "NRML", "order_type": "MARKET", "transaction_type": "SELL",
                 "order_reference_id": g.reference_id("r1")}


def test_limit_and_stop_orders_carry_price_and_trigger():
    lim = g.place_params(order(side=Side.BUY, otype=OrderType.LIMIT, price=20.05), SYMS[SHORT])
    assert lim["price"] == 20.05 and "trigger_price" not in lim
    sl = g.place_params(order(side=Side.BUY, otype=OrderType.SL, price=30.0, trigger=29.5), SYMS[SHORT])
    assert (sl["order_type"], sl["price"], sl["trigger_price"]) == ("SL", 30.0, 29.5)
    assert g.place_params(Order(SHORT, Side.BUY, 65, OrderType.MARKET, Product.INTRADAY, "s", "v"), SYMS[SHORT])["product"] == "MIS"


@pytest.mark.parametrize("bad, why", [
    (dict(qty=60), "whole number of lots"),
    (dict(qty=65 * 28), "freeze quantity"),                              # 1820 > 1755
    (dict(otype=OrderType.LIMIT, price=20.03), "tick"),
    (dict(otype=OrderType.LIMIT, price=0.0), "positive"),
])
def test_orders_that_fail_our_sanity_checks_are_never_built(bad, why):
    with pytest.raises(g.OrderRejected, match=why):
        g.place_params(order(**bad), SYMS[SHORT])


def test_side_flags_never_block_an_order_so_an_exit_cannot_be_trapped():
    g.place_params(order(inst=LONG, side=Side.SELL), SYMS[LONG])             # sell_allowed is 0 for this contract


# --- status and updates in ------------------------------------------------------------------------

@pytest.mark.parametrize("status, filled, expected", [
    ("NEW", 0, OrderStatus.OPEN), ("ACKED", 0, OrderStatus.OPEN), ("APPROVED", 0, OrderStatus.OPEN),
    ("TRIGGER_PENDING", 0, OrderStatus.OPEN), ("CANCELLATION_REQUESTED", 0, OrderStatus.OPEN),
    ("APPROVED", 25, OrderStatus.PARTIALLY_FILLED), ("MODIFICATION_REQUESTED", 25, OrderStatus.PARTIALLY_FILLED),
    ("EXECUTED", 65, OrderStatus.FILLED), ("COMPLETED", 65, OrderStatus.FILLED),
    ("EXECUTED", 30, OrderStatus.PARTIALLY_FILLED),
    ("CANCELLED", 0, OrderStatus.CANCELLED), ("CANCELLED", 30, OrderStatus.CANCELLED),
    ("REJECTED", 0, OrderStatus.REJECTED), ("FAILED", 0, OrderStatus.REJECTED),
])
def test_status_mapping(status, filled, expected):
    assert g.order_status(status, filled, 65) is expected


def test_unrecognised_or_inconsistent_status_fails_loudly():
    with pytest.raises(g.UnknownStatus):
        g.order_status("SOMETHING_NEW", 0, 65)
    with pytest.raises(g.UnknownStatus):
        g.order_status("EXECUTED", 0, 65)


def test_update_from_a_rest_status_and_from_the_live_feed():
    rest = {"groww_order_id": "GMK1", "order_status": "EXECUTED", "filled_quantity": 65, "quantity": 65,
            "average_fill_price": 19.8, "remark": "ok", "order_reference_id": "tlx"}
    feed = {"growwOrderId": "GMK1", "orderStatus": "APPROVED", "filledQty": 25, "qty": 65, "avgFillPrice": 19.8}
    a, b = g.order_update(rest, "r1", 5), g.order_update(feed, "r1", 6)
    assert (a.status, a.filled_qty, a.avg_price, a.broker_order_id, a.message) == (OrderStatus.FILLED, 65, 19.8, "GMK1", "ok")
    assert (b.status, b.filled_qty) == (OrderStatus.PARTIALLY_FILLED, 25)


def test_fill_tracker_emits_each_slice_once_and_keeps_the_average_right():
    t = g.FillTracker()
    f1 = t.update("r1", SHORT, Side.SELL, 25, 20.0, 1)
    f2 = t.update("r1", SHORT, Side.SELL, 65, 19.5, 2)                   # average now 19.5 over 65
    assert (f1.qty, f1.price, f1.fill_id) == (25, 20.0, "r1-25")
    assert (f2.qty, f2.fill_id) == (40, "r1-65")
    assert round(f1.qty * f1.price + f2.qty * f2.price, 2) == round(65 * 19.5, 2)
    assert t.update("r1", SHORT, Side.SELL, 65, 19.5, 3) is None         # same report again: nothing new


def test_fill_tracker_restart_and_inconsistency():
    t = g.FillTracker()
    t.seed("r1", 25, 20.0)                                               # already booked before the restart
    assert t.update("r1", SHORT, Side.SELL, 25, 20.0, 1) is None
    assert t.update("r1", SHORT, Side.SELL, 65, 19.5, 2).qty == 40
    with pytest.raises(g.InconsistentFill):
        t.update("r1", SHORT, Side.SELL, 30, 19.5, 3)                    # quantity went backwards
    with pytest.raises(g.InconsistentFill):
        g.FillTracker().update("r2", SHORT, Side.SELL, 65, None, 1)


# --- positions ----------------------------------------------------------------------------------

def row(sym, qty, credit=0, debit=0, cf_credit=0, cf_debit=0, price=19.8):
    return {"trading_symbol": sym, "quantity": qty, "net_price": price, "credit_quantity": credit,
            "debit_quantity": debit, "carry_forward_credit_quantity": cf_credit, "carry_forward_debit_quantity": cf_debit}


def test_signed_positions_from_todays_trades_and_from_carried_forward_ones():
    rows = [row("NIFTY26N0324400PE", -65, debit=65), row("NIFTY26N0323900PE", 65, credit=65, price=5.2),
            row("NIFTY26N0324400PE", -65, cf_debit=65)]
    got = g.positions(rows[:2], BY_NAME)
    assert [(p.instrument, p.qty, p.avg_price) for p in got] == [(SHORT, -65, 19.8), (LONG, 65, 5.2)]
    assert g.positions(rows[2:], BY_NAME)[0].qty == -65


def test_flat_rows_are_dropped_and_ambiguous_ones_fail_loudly():
    assert g.positions([row("NIFTY26N0324400PE", 0, credit=65, debit=65)], BY_NAME) == []
    with pytest.raises(g.PositionAmbiguous, match="carried-forward"):
        g.positions([row("NIFTY26N0324400PE", -65, cf_debit=65, debit=65)], BY_NAME)    # quantity ignores the carry


def test_a_position_in_an_unknown_contract_is_untagged_not_skipped():
    with pytest.raises(g.UnknownSymbol, match="Rule #1"):
        g.positions([row("RELIANCE26NFUT", 250, credit=250)], BY_NAME)


# --- margin -------------------------------------------------------------------------------------

def test_margin_basket_request_and_parsing():
    legs = [(order(inst=LONG, side=Side.BUY), SYMS[LONG]), (order(otype=OrderType.LIMIT, price=19.8), SYMS[SHORT])]
    req = g.margin_request(legs)
    assert req["segment"] == "FNO" and [x["transaction_type"] for x in req["orders"]] == ["BUY", "SELL"]
    assert "price" not in req["orders"][0] and req["orders"][1]["price"] == 19.8
    m = g.parse_margin({"total_requirement": 61000.0, "span_required": 58000.0, "exposure_required": 2000.0,
                        "option_buy_premium": 340.0, "brokerage_and_charges": 45.0})
    a = g.parse_available({"clear_cash": 150000.0, "net_margin_used": 0.0,
                           "fno_margin_details": {"option_sell_balance_available": 150000.0, "option_buy_balance_available": 150000.0}})
    assert (m.total, m.span, m.option_premium) == (61000.0, 58000.0, 340.0) and a.option_sell == 150000.0
    assert g.margin_ok(m, a) and not g.margin_ok(m, g.parse_available({"fno_margin_details": {"option_sell_balance_available": 65000.0}}))


# --- errors -------------------------------------------------------------------------------------

def test_error_classes_decide_what_happens_next():
    mk = lambda n: type(n, (Exception,), {})()
    assert g.classify_error(mk("GrowwAPITimeoutException")) == "unknown"        # may exist: look up by reference first
    assert g.classify_error(ConnectionResetError()) == "unknown"
    assert g.classify_error(mk("SomethingNobodyExpected")) == "unknown"          # default is the safe one
    assert g.classify_error(mk("GrowwAPIRateLimitException")) == "retry"
    assert g.classify_error(mk("GrowwAPIBadRequestException")) == "rejected"
    assert g.classify_error(mk("GrowwAPIAuthorisationException")) == "auth"
