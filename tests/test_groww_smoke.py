from datetime import date

import pytest

from tests.test_groww_execution import FakeGroww
from tests.test_groww_orders import SHORT, SYMS
from tradeloop.groww_smoke import READ_ONLY, ReadOnlyApi, smoke, spread_orders


def test_the_read_only_wrapper_blocks_every_method_that_could_trade():
    ro = ReadOnlyApi(FakeGroww())
    for name in ("place_order", "cancel_order", "modify_order", "create_smart_order", "cancel_smart_order"):
        with pytest.raises(PermissionError, match="read-only"):
            getattr(ro, name)
    assert ro.get_positions_for_user(segment="FNO") == {"positions": []}
    assert READ_ONLY == {"get_positions_for_user", "get_available_margin_details", "get_order_list", "get_order_margin_details"}


class MarginApi(FakeGroww):
    def get_available_margin_details(self, **kw):
        self.calls.append(("available_margin", kw))
        return {"clear_cash": 100000.0, "net_margin_used": 0.0,
                "fno_margin_details": {"option_sell_balance_available": 100000.0, "option_buy_balance_available": 100000.0}}

    def get_order_margin_details(self, **kw):
        self.calls.append(("order_margin", kw))
        sells = [o for o in kw["orders"] if o["transaction_type"] == "SELL"]
        return {"total_requirement": 90000.0 if len(kw["orders"]) == 1 else 60000.0, "span_required": 1.0,
                "exposure_required": 1.0, "option_buy_premium": 0.0, "brokerage_and_charges": 20.0 * len(sells)}


def test_the_smoke_run_makes_only_read_calls_and_reports_the_hedge_benefit():
    api = MarginApi()
    report = "\n".join(smoke(api, SYMS, spot=24800.0, lots=1, today=date(2026, 11, 2), prices={}))
    assert {name for name, _ in api.calls} <= {"positions", "available_margin", "order_list", "order_margin"}
    assert "hedge benefit: Rs 30,000" in report and "Nothing unexpected" not in report      # no open positions: noted
    assert "clear cash Rs 100,000.00" in report


def test_a_failing_section_does_not_hide_the_others():
    api = MarginApi()
    api.get_available_margin_details = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    report = "\n".join(smoke(api, SYMS, spot=24800.0, lots=1, today=date(2026, 11, 2), prices={}))
    assert "FAILED: RuntimeError: boom" in report and "hedge benefit" in report


def test_spread_is_picked_from_the_nearest_expiry_around_2_5_and_4_5_percent():
    long_o, short_o = spread_orders(SYMS, 25000.0, 1, date(2026, 11, 2))
    assert short_o.instrument == SHORT and short_o.side.value == "SELL" and long_o.side.value == "BUY"
