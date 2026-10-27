from datetime import date, datetime

import pytest

from tradeloop.charges import default_charges
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Side
from tradeloop.ledger import CASH, IST, Ledger, Posting, Unbalanced
from tradeloop.lot_sizes import default_lot_sizes

OCT20 = date(2026, 10, 20)


def ms(d=OCT20, hh=10, mm=0):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp() * 1000)


def opt(strike=25000, kind="CE", expiry=OCT20, exchange="NSE", underlying="NIFTY"):
    return InstrumentId.option(exchange, underlying, expiry, float(strike), kind)


_n = 0


def fill(side, qty, price, instrument=None, ref=None, ts=None):
    global _n
    _n += 1
    return Fill(f"f{_n}", ref or f"o{_n}", instrument or opt(), side, qty, price, ts or ms())


def assert_balanced(led):
    assert sum(led.balances().values()) == 0
    for e in led.entries:
        assert sum(p.amount for p in e.postings) == 0


# --- charges, worked by hand (see README) -----------------------------------------------

def test_charges_on_a_nifty_option_round_trip_match_hand_calculation():
    c = default_charges()
    buy = c.trade(OCT20, opt(), Side.BUY, 650_000)          # 65 @ 100.00
    sell = c.trade(OCT20, opt(), Side.SELL, 780_000)        # 65 @ 120.00
    assert buy == {"brokerage": 2000, "stt": 0, "exchange": 228, "sebi": 1, "ipft": 0, "stamp": 20, "gst": 401}
    assert sell == {"brokerage": 2000, "stt": 1200, "exchange": 273, "sebi": 1, "ipft": 0, "stamp": 0, "gst": 409}


def test_matches_growws_charges_calculator():
    # Groww, Oct 2026: 65 NIFTY options, buy 1000, sell 1500 -> total Rs 262.50
    c = default_charges()
    buy = c.trade(OCT20, opt(), Side.BUY, 6_500_000)
    sell = c.trade(OCT20, opt(), Side.SELL, 9_750_000)
    both = {k: buy[k] + sell[k] for k in buy}
    groww = {"brokerage": 4000, "stt": 14600, "exchange": 5692, "sebi": 16, "ipft": 0, "stamp": 195, "gst": 1747}
    for k, v in groww.items():
        assert abs(both[k] - v) <= 1, k        # we round per fill, Groww on the round trip
    assert abs(sum(both.values()) - 26250) <= 2


@pytest.mark.parametrize("day,stt", [("2023-03-31", 5000), ("2023-04-01", 6300),     # 62.50 -> 63
                                     ("2024-10-01", 10000), ("2026-04-01", 15000)])
def test_option_stt_follows_the_rate_in_force(day, stt):
    d = date.fromisoformat(day)
    assert default_charges().trade(d, opt(expiry=d), Side.SELL, 10_000_000)["stt"] == stt   # Rs 1 lakh


def test_bse_uniform_fee_and_brokerage_once_per_order():
    c = default_charges().trade(date(2024, 10, 1), opt(80000, expiry=date(2024, 10, 4), exchange="BSE",
                                                      underlying="SENSEX"), Side.BUY, 10_000_000, False)
    assert c["exchange"] == 3250 and c["brokerage"] == 0 and c["ipft"] == 0


# --- ledger ------------------------------------------------------------------------------

def test_round_trip_pnl_and_cash():
    led = Ledger()
    led.deposit(100_000, 0)
    led.record_fill(fill(Side.BUY, 65, 100.0))
    led.record_fill(fill(Side.SELL, 65, 120.0))
    assert led.realised_pnl() == 130_000 and led.total_charges() == 6_533
    assert led.net_pnl() == 123_467                               # Rs 1,234.67
    assert led.balance(CASH) == 10_000_000 + 123_467
    assert led.positions() == []
    assert_balanced(led)


def test_partial_close_uses_average_cost():
    led = Ledger()
    i = opt(expiry=date(2025, 6, 26))
    led.record_fill(fill(Side.BUY, 75, 100.0, i))
    led.record_fill(fill(Side.BUY, 75, 110.0, i))
    led.record_fill(fill(Side.SELL, 75, 120.0, i))
    assert led.realised_pnl() == 15 * 75 * 100
    [p] = led.positions()
    assert (p.qty, p.avg_price) == (75, 105.0)
    assert_balanced(led)


def test_selling_through_zero_closes_then_opens_short():
    led = Ledger()
    led.record_fill(fill(Side.BUY, 65, 100.0))
    led.record_fill(fill(Side.SELL, 130, 90.0))
    assert led.realised_pnl() == -10 * 65 * 100
    [p] = led.positions()
    assert (p.qty, p.avg_price) == (-65, 90.0)
    led.record_fill(fill(Side.BUY, 65, 80.0))                     # cover the short
    assert led.realised_pnl() == (-10 + 10) * 65 * 100 and led.positions() == []
    assert_balanced(led)


def test_brokerage_charged_once_per_order_across_partial_fills():
    led = Ledger()
    led.record_fill(fill(Side.BUY, 65, 100.0, ref="order-1"))
    led.record_fill(fill(Side.BUY, 65, 100.05, ref="order-1"))
    assert led.balance("charges:brokerage") == 2000


def test_repeated_fill_is_ignored():
    led = Ledger()
    f = fill(Side.BUY, 65, 100.0)
    led.record_fill(f)
    assert led.record_fill(f) == []
    assert led.positions()[0].qty == 65


def test_short_option_expiring_worthless_keeps_the_premium():
    led = Ledger()
    i = opt(25500, "CE")
    led.record_fill(fill(Side.SELL, 65, 50.0, i))
    led.settle_expiry(OCT20, "NIFTY", 25400.0, ms(hh=15, mm=40))
    assert led.realised_pnl() == 50 * 65 * 100 and led.positions() == []
    assert led.balance("charges:stt") == 500                     # only the sale: 0.15% of 3250 = 4.875 -> 5
    assert_balanced(led)


def test_long_in_the_money_option_settles_at_intrinsic_value_and_pays_exercise_stt():
    led = Ledger()
    led.record_fill(fill(Side.BUY, 65, 100.0, opt(25000, "CE")))
    led.record_fill(fill(Side.BUY, 65, 30.0, opt(25000, "PE")))
    led.settle_expiry(OCT20, "NIFTY", 25150.0, ms(hh=15, mm=40))
    assert led.realised_pnl() == (150 - 100) * 65 * 100 - 30 * 65 * 100
    assert led.balance("charges:stt") == 1500                    # 0.15% of 150 x 65 = 14.625 -> 15
    assert led.positions() == []
    assert_balanced(led)


def test_unbalanced_entry_is_refused():
    with pytest.raises(Unbalanced):
        Ledger().post(0, "bad", [Posting(CASH, 100), Posting("capital", -99)])


# --- lot sizes ---------------------------------------------------------------------------

@pytest.mark.parametrize("index,expiry,lot", [
    ("NSE|NIFTY", "2021-07-22", 75), ("NSE|NIFTY", "2021-07-29", 50), ("NSE|NIFTY", "2024-05-02", 25),
    ("NSE|NIFTY", "2025-01-30", 25), ("NSE|NIFTY", "2025-02-06", 75), ("NSE|NIFTY", "2026-01-06", 65),
    ("BSE|SENSEX", "2025-01-03", 10), ("BSE|SENSEX", "2025-01-07", 20), ("BSE|SENSEX", "2025-01-28", 10),
])
def test_lot_sizes_by_expiry(index, expiry, lot):
    ex, u = index.split("|")
    assert default_lot_sizes().lot_size(opt(expiry=date.fromisoformat(expiry), exchange=ex, underlying=u)) == lot
