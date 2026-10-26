import ast
from datetime import date
from pathlib import Path

import pytest

from tradeloop.core.bus import Bus, Topic
from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.interfaces import Execution, MarketData
from tradeloop.core.types import Order, OrderStatus, OrderType, Product, Side, Tick

NIFTY_CE = InstrumentId.option("NSE", "NIFTY", date(2026, 10, 27), 25500, "CE")


# --- neutral IDs ------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "NSE|NIFTY|IDX",
    "NSE|RELIANCE|EQ",
    "NSE|NIFTY|2026-10-27|FUT",
    "NSE|NIFTY|2026-10-27|25500|CE",
    "BSE|SENSEX|2026-10-29|82000|PE",
    "NSE|NIFTY|2026-10-27|25512.5|CE",
])
def test_id_round_trip(text):
    assert str(InstrumentId.parse(text)) == text


def test_id_is_hashable_and_equal_by_value():
    a = InstrumentId.parse("NSE|NIFTY|2026-10-27|25500|CE")
    assert a == NIFTY_CE and {a: 1}[NIFTY_CE] == 1
    assert NIFTY_CE.kind is Kind.CE and NIFTY_CE.kind.is_option


@pytest.mark.parametrize("bad", [
    "NSE|NIFTY|2026-10-27|CE",        # option without strike
    "NSE|NIFTY|25500|IDX",             # index with extra part
    "NSE|NIFTY|XYZ",                   # unknown kind
    "NSE|NIFTY|27-10-2026|25500|CE",   # wrong date format
])
def test_id_rejects_bad_text(bad):
    with pytest.raises(ValueError):
        InstrumentId.parse(bad)


def test_id_rejects_inconsistent_fields():
    with pytest.raises(ValueError):
        InstrumentId("NSE", "NIFTY", Kind.CE, date(2026, 10, 27))  # no strike
    with pytest.raises(ValueError):
        InstrumentId("NSE", "NIFTY", Kind.IDX, date(2026, 10, 27))  # index with expiry


# --- orders -----------------------------------------------------------------

def make_order(**kw):
    base = dict(instrument=NIFTY_CE, side=Side.SELL, qty=75, order_type=OrderType.LIMIT,
                product=Product.INTRADAY, strategy_id="expiry-v1", config_version="abc123", price=42.5)
    base.update(kw)
    return Order(**base)


def test_order_gets_unique_client_ref():
    assert make_order().client_ref != make_order().client_ref


def test_order_requires_strategy_and_version():
    with pytest.raises(ValueError):
        make_order(strategy_id="")
    with pytest.raises(ValueError):
        make_order(config_version="")


def test_order_validates_prices_and_qty():
    with pytest.raises(ValueError):
        make_order(price=None)                                  # LIMIT without price
    with pytest.raises(ValueError):
        make_order(order_type=OrderType.SL_M, price=None)       # SL_M without trigger
    with pytest.raises(ValueError):
        make_order(qty=0)
    make_order(order_type=OrderType.SL_M, price=None, trigger=60.0)


def test_side_sign_and_final_status():
    assert Side.BUY.sign == 1 and Side.SELL.sign == -1
    assert OrderStatus.FILLED.is_final and not OrderStatus.OPEN.is_final


# --- bus --------------------------------------------------------------------

def test_bus_delivers_in_order_and_isolates_failures():
    bus, seen = Bus(), []

    def boom(_):
        raise RuntimeError("bad subscriber")

    bus.subscribe(Topic.TICK, lambda t: seen.append(("a", t.ltp)))
    bus.subscribe(Topic.TICK, boom)
    bus.subscribe(Topic.TICK, lambda t: seen.append(("b", t.ltp)))
    bus.publish(Topic.TICK, Tick(NIFTY_CE, 1, ltp=10.0))
    bus.publish(Topic.FILL, object())            # no subscribers: fine
    assert seen == [("a", 10.0), ("b", 10.0)]
    assert bus.errors == 1


# --- interfaces -------------------------------------------------------------

class FakeBroker:
    def instruments(self): return [NIFTY_CE]
    def subscribe(self, ids): pass
    def on_tick(self, handler): pass
    def run(self): pass
    def stop(self): pass
    def place(self, order): pass
    def cancel(self, client_ref): pass
    def cancel_all(self): pass
    def positions(self): return []
    def on_order_update(self, handler): pass
    def on_fill(self, handler): pass


def test_fake_broker_satisfies_both_interfaces():
    b = FakeBroker()
    assert isinstance(b, MarketData) and isinstance(b, Execution)


# --- architecture guard -----------------------------------------------------

BROKER_SDKS = {"growwapi", "kiteconnect"}


def test_core_imports_no_broker_sdk():
    core = Path(__file__).resolve().parents[1] / "tradeloop" / "core"
    for path in core.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in BROKER_SDKS, f"{path.name} imports {name}"
