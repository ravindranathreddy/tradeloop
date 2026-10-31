"""Pre-trade margin check against a fake margin API (numbers from the 31 Oct 2026 read-only quote)."""
import logging

from tests.test_groww_execution import FakeGroww
from tests.test_groww_orders import LONG, SHORT, SYMS, order
from tradeloop import recorder
from tradeloop.core.types import OrderStatus, Position, Side
from tradeloop.margin_guard import MarginGuard
from tradeloop.risk import RiskGate

HEDGED, NAKED = 60_328.0, 124_714.0


class MarginApi(FakeGroww):
    def __init__(self, available=300_000.0):
        super().__init__()
        self.available, self.quotes, self.down = available, [], False

    def get_order_margin_details(self, segment, orders, timeout=None):
        if self.down:
            raise ConnectionError("down")
        self.quotes.append(orders)
        sells = [o for o in orders if o["transaction_type"] == "SELL"]
        buys = [o for o in orders if o["transaction_type"] == "BUY"]
        total = (HEDGED if buys else NAKED) if sells else sum(o["price"] * o["quantity"] for o in buys)
        return {"total_requirement": total, "span_required": 0, "exposure_required": 0, "option_buy_premium": 0,
                "brokerage_and_charges": 0}

    def get_available_margin_details(self, timeout=None):
        if self.down:
            raise ConnectionError("down")
        return {"clear_cash": self.available, "net_margin_used": 0,
                "fno_margin_details": {"option_sell_balance_available": self.available, "option_buy_balance_available": self.available}}


PRICES = {SHORT: 19.7, LONG: 3.7}


def guard(api, held=(), **kw):
    book = [Position(i, q, 10.0) for i, q in held]
    return MarginGuard(api, SYMS, positions=lambda: book, price=PRICES.get, **kw)


def test_buying_the_protection_to_start_a_spread_is_allowed_with_enough_free_margin():
    api = MarginApi()
    assert guard(api).check(order(LONG, Side.BUY, ref="a")) is None
    assert len(api.quotes) == 1 and len(api.quotes[0]) == 1               # the book after: just the long put


def test_the_short_is_quoted_as_a_basket_with_the_protection_already_held():
    api = MarginApi()
    assert guard(api, [(LONG, 65)]).check(order(SHORT, Side.SELL, ref="b")) is None
    after, before = api.quotes[0], api.quotes[1]
    assert sorted(o["transaction_type"] for o in after) == ["BUY", "SELL"]  # hedged basket: Groww applies the hedge benefit
    assert [o["transaction_type"] for o in before] == ["BUY"]
    assert all(o["price"] > 0 for o in after)                              # the SDK needs a price on every leg


def test_a_naked_short_is_blocked_by_the_cap_even_with_plenty_of_cash():
    api = MarginApi(available=1_000_000)
    why = guard(api).check(order(SHORT, Side.SELL, ref="c"))              # the ledger shows no protection
    assert "exceed the live cap of Rs 100,000" in why and "124,714" in why


def test_not_enough_free_margin_for_the_extra_requirement_refuses():
    api = MarginApi(available=70_000)
    why = guard(api, [(LONG, 65)]).check(order(SHORT, Side.SELL, ref="d"))
    assert "free margin Rs 70,000 is below the Rs 90," in why              # about Rs 60k x 1.5 for expiry-day margin


def test_a_new_spread_needs_the_entry_floor_before_the_protection_is_even_bought():
    api = MarginApi(available=80_000)
    assert "needed to start a spread" in guard(api).check(order(LONG, Side.BUY, ref="e"))
    assert guard(MarginApi(available=95_000)).check(order(LONG, Side.BUY, ref="e2")) is None


def test_no_margin_figure_means_no_new_risk_and_nothing_raises():
    api = MarginApi()
    api.down = True
    assert "margin check failed (ConnectionError" in guard(api).check(order(LONG, Side.BUY, ref="f"))
    assert "no reference price" in MarginGuard(MarginApi(), SYMS, positions=list, price=lambda i: None).check(order(LONG, Side.BUY, ref="f2"))
    assert "not a Groww contract" in MarginGuard(MarginApi(), {}, positions=list, price=PRICES.get).check(order(LONG, Side.BUY, ref="f3"))


def test_closing_orders_are_never_refused_even_when_margin_cannot_be_asked():
    api = MarginApi()
    api.down = True
    book = [(SHORT, -65), (LONG, 65)]
    assert guard(api, book).check(order(SHORT, Side.BUY, ref="g1")) is None      # buy the short back
    assert guard(api, book).check(order(LONG, Side.SELL, ref="g2")) is None      # then sell the protection
    assert guard(api, [(SHORT, -130)]).check(order(SHORT, Side.BUY, ref="g3")) is None   # a partial close


def test_closing_more_than_is_held_is_new_risk_and_gets_checked():
    api = MarginApi()
    api.down = True
    assert "margin check failed" in guard(api, [(SHORT, -65)]).check(order(SHORT, Side.BUY, qty=130, ref="h"))


def test_the_risk_gate_turns_a_refusal_into_a_risk_rejection(caplog):
    api = MarginApi(available=1_000)
    sent, updates = [], []
    inner = type("Inner", (), {"place": lambda s, o: sent.append(o), "on_order_update": lambda s, h: None,
                               "cancel": lambda s, r: None, "cancel_all": lambda s: None, "positions": lambda s: [],
                               "on_fill": lambda s, h: None})()
    gate = RiskGate(inner, max_lots=1, lot_size=lambda i: 65, fresh=lambda i: True, clock_ms=lambda: 1,
                    margin=guard(api).check)
    gate.on_order_update(updates.append)
    with caplog.at_level(logging.WARNING):
        gate.place(order(LONG, Side.BUY, ref="i"))
    assert sent == [] and updates[0].status is OrderStatus.REJECTED_RISK
    assert "ALERT risk rejected" in caplog.text and "needed to start a spread" in caplog.text


def test_the_recorder_wires_the_guard_into_live_traders_with_the_config_numbers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.test_live_wiring import cfg_for
    monkeypatch.setattr(recorder, "load_symbols", lambda api: SYMS)
    rec = SimpleNamespace(bus=SimpleNamespace(subscribe=lambda *a: None))
    (t,) = recorder.live_traders(cfg_for(tmp_path, margin_buffer=2.0, max_margin=80_000, margin_min_free=50_000), rec, None, MarginApi())
    guard_ = t.gate.margin.__self__
    assert (guard_.buffer, guard_.max_margin, guard_.min_free) == (2.0, 80_000.0, 50_000.0)
    (t,) = recorder.live_traders(cfg_for(tmp_path), rec, None, MarginApi())
    assert (t.gate.margin.__self__.max_margin, t.gate.margin.__self__.min_free) == (100_000.0, 90_000.0)   # defaults match config.yaml


def test_the_committed_config_has_the_margin_limits():
    from pathlib import Path

    import yaml
    live = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())["live"]
    assert (live["margin_buffer"], live["margin_min_free"], live["max_margin"]) == (1.5, 90000, 100000)
