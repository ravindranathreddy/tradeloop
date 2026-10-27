from datetime import date, datetime

import pandas as pd
import pytest

from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Order, OrderType, Product, Side
from tradeloop.fill_model import FillModel
from tradeloop.ledger import IST

DAY = date(2026, 10, 20)
I = InstrumentId.option("NSE", "NIFTY", DAY, 25000.0, "CE")
LOT = 65


def ms(hh, mm):
    return int(datetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=IST).timestamp() * 1000)


def candles(rows):
    """rows: (hh, mm, open, high, low, close, volume)"""
    return pd.DataFrame([(ms(h, m), o, hi, lo, c, v) for h, m, o, hi, lo, c, v in rows],
                        columns=["ts_ms", "open", "high", "low", "close", "volume"])


def order(side=Side.BUY, qty=65, order_type=OrderType.MARKET, price=None, trigger=None):
    return Order(I, side, qty, order_type, Product.INTRADAY, "test", "abc123", price, trigger, client_ref="r1")


M = FillModel(slippage_ticks=2, slippage_pct=0.5, max_volume_share=0.1, max_wait_minutes=5)


def test_market_order_fills_next_minute_open_plus_slippage():
    c = candles([(10, 0, 99, 101, 98, 100, 6500), (10, 1, 100, 102, 99, 101, 6500)])
    [f] = M.fill(order(), ms(10, 1), c, LOT)                # decided after the 10:00 candle closed
    assert (f.ts_ms, f.price, f.qty) == (ms(10, 1), 100.5, 65)    # 0.5% of 100 > 2 ticks
    [s] = M.fill(order(Side.SELL), ms(10, 1), c, LOT)
    assert s.price == 99.5


def test_cheap_options_pay_at_least_two_ticks_and_round_against_us():
    c = candles([(10, 1, 1.0, 1.2, 0.9, 1.1, 65000)])
    assert M.fill(order(), ms(10, 1), c, LOT)[0].price == 1.1
    assert M.fill(order(Side.SELL), ms(10, 1), c, LOT)[0].price == 0.9


def test_no_fill_without_volume_and_order_spreads_over_minutes():
    c = candles([(10, 1, 100, 100, 100, 100, 0),            # nothing traded
                 (10, 2, 100, 101, 99, 100, 1300),           # 10% = 130 = 2 lots
                 (10, 3, 101, 102, 100, 101, 650)])          # 10% = 65 = 1 lot
    fills = M.fill(order(qty=195), ms(10, 1), c, LOT)
    assert [(f.ts_ms, f.qty) for f in fills] == [(ms(10, 2), 130), (ms(10, 3), 65)]
    assert [f.fill_id for f in fills] == ["r1-1", "r1-2"]


def test_market_order_gives_up_after_the_wait():
    c = candles([(10, 1 + i, 100, 100, 100, 100, 650) for i in range(10)])
    fills = M.fill(order(qty=650), ms(10, 1), c, LOT)
    assert sum(f.qty for f in fills) == 5 * 65


def test_limit_needs_the_price_to_trade_through():
    c = candles([(10, 1, 101, 102, 100.0, 101, 6500),       # touches 100, not through
                 (10, 2, 100.5, 101, 99.9, 100, 6500)])      # through
    [f] = M.fill(order(order_type=OrderType.LIMIT, price=100.0), ms(10, 1), c, LOT)
    assert (f.ts_ms, f.price) == (ms(10, 2), 100.0)          # capped at the limit


def test_stop_triggers_then_fills_worse_of_open_and_trigger():
    c = candles([(10, 1, 100, 101, 95, 96, 6500),            # sell stop at 90 not hit
                 (11, 30, 92, 93, 85, 86, 6500)])            # hit; opened above the trigger
    [f] = M.fill(order(Side.SELL, order_type=OrderType.SL_M, trigger=90.0), ms(10, 1), c, LOT)
    assert (f.ts_ms, f.price) == (ms(11, 30), 89.55)         # 90 - 0.45, rounded down to tick
    gap = candles([(10, 1, 80, 82, 78, 79, 6500)])           # gapped through the stop
    assert M.fill(order(Side.SELL, order_type=OrderType.SL_M, trigger=90.0), ms(10, 1), gap, LOT)[0].price == 79.6


def test_nothing_fills_after_the_close():
    c = candles([(15, 40, 100, 100, 100, 100, 6500)])        # F&O closes 15:40 on this date
    assert M.fill(order(), ms(15, 39), c, LOT) == []


def test_quantity_must_be_whole_lots():
    with pytest.raises(ValueError):
        M.fill(order(qty=70), ms(10, 1), candles([]), LOT)
