from datetime import date, datetime, time

import pandas as pd
import pytest

import tradeloop.engine as engine_mod
from tradeloop.backtest import run_week
from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST
from tradeloop.core.types import Side
from tradeloop.fill_model import FillModel

EXP, MON, PREV = date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 13)   # NIFTY lot 65
NIFTY = InstrumentId.index("NSE", "NIFTY")
SHORT = f"NSE|NIFTY|{EXP}|24400|PE"          # ~2.4% below 25000
LONG = f"NSE|NIFTY|{EXP}|23900|PE"           # ~4.4% below
SHORT_CE = f"NSE|NIFTY|{EXP}|25600|CE"       # ~2.4% above
LONG_CE = f"NSE|NIFTY|{EXP}|26100|CE"        # ~4.4% above
CFG = {"strategy_id": "pcs", "strategy": "put_credit_spread", "lots": 1, "entry_time": "09:20",
       "enter_within_days": 2, "short_otm_pct": 2.5, "long_otm_pct": 4.5}
NO_SLIP = FillModel(slippage_ticks=0, slippage_pct=0)


def ms(d, hh, mm):
    return int(datetime.combine(d, time(hh, mm), IST).timestamp() * 1000)


def candles(long_volume=6500.0):
    rows = []
    for d in (MON, EXP):
        for m in range(9 * 60 + 15, 15 * 60 + 40):
            ts = ms(d, m // 60, m % 60)
            rows.append(("NSE|NIFTY|IDX", ts, 25000.0, 25000.0, 25000.0, 25000.0, None, None))
            rows.append((SHORT, ts, 20.0, 20.0, 20.0, 20.0, 6500.0, 1000.0))
            rows.append((LONG, ts, 5.0, 5.0, 5.0, 5.0, long_volume, 1000.0))
            rows.append((SHORT_CE, ts, 18.0, 18.0, 18.0, 18.0, 6500.0, 1000.0))
            rows.append((LONG_CE, ts, 4.0, 4.0, 4.0, 4.0, long_volume, 1000.0))
    return pd.DataFrame(rows, columns=["instrument", "ts_ms", "open", "high", "low", "close", "volume", "oi"])


def run(official_close, long_volume=6500.0, cfg=CFG):
    fills = []
    orig = engine_mod.Engine._on_fill
    engine_mod.Engine._on_fill = lambda self, f: (fills.append(f), orig(self, f))
    try:
        rows = run_week(cfg, "v1", NIFTY, EXP, candles(long_volume), official_close, NO_SLIP, PREV)
    finally:
        engine_mod.Engine._on_fill = orig
    return rows, fills


def test_buys_protection_first_then_sells_and_keeps_the_credit_in_a_calm_week():
    rows, fills = run(25000.0)
    assert [(str(f.instrument), f.side, f.ts_ms) for f in fills] == [
        (LONG, Side.BUY, ms(MON, 9, 20)), (SHORT, Side.SELL, ms(MON, 9, 21))]
    mon, tue = rows
    assert mon["held_overnight"] == 2 and tue["settled_at_expiry"] == 2
    assert sum(r["gross"] for r in rows) == (20 - 5) * 65                 # both expire worthless


def test_loss_is_capped_by_the_protection_in_a_crash():
    rows, _ = run(22000.0)                                                 # NIFTY -12%
    # short pays 2400, long pays 1900: net 500 per unit, minus the 15 credit
    assert sum(r["gross"] for r in rows) == -(500 - 15) * 65


def test_nothing_is_sold_if_the_protection_does_not_fill():
    rows, fills = run(22000.0, long_volume=0.0)
    assert fills == [] and sum(r["gross"] for r in rows) == 0


def test_config_must_put_protection_further_out():
    from tradeloop.strategies.credit_spreads import PutCreditSpread
    with pytest.raises(ValueError):
        PutCreditSpread({**CFG, "short_otm_pct": 5, "long_otm_pct": 4})


def test_call_side_mirrors_the_put_side():
    cfg = {**CFG, "strategy": "credit_spread", "option_type": "CE"}
    rows, fills = run(25000.0, cfg=cfg)
    assert [(str(f.instrument), f.side) for f in fills] == [(LONG_CE, Side.BUY), (SHORT_CE, Side.SELL)]
    assert sum(r["gross"] for r in rows) == (18 - 4) * 65
    rows, _ = run(28000.0, cfg=cfg)                                        # NIFTY +12%
    assert sum(r["gross"] for r in rows) == -(500 - 14) * 65


@pytest.mark.parametrize("close,expected", [
    (25000.0, (15 + 14) * 65),                       # calm: keep both credits
    (22000.0, -(500 - 15) * 65 + 14 * 65),           # crash: put side capped, call side keeps its credit
    (28000.0, -(500 - 14) * 65 + 15 * 65),           # rally: the mirror image
])
def test_iron_condor_only_one_side_can_lose(close, expected):
    rows, fills = run(close, cfg={**CFG, "strategy": "iron_condor"})
    assert len(fills) == 4 and sum(r["gross"] for r in rows) == expected


class _Ctx:
    """Just enough context for the entry-day check."""
    def __init__(self, today, expiry, prev):
        self.today, self.expiry, self._prev = today, expiry, prev
        self.days_to_expiry = (expiry - today).days
        self.time = time(9, 20)
        self.orders = []

    def previous_trading_day(self):
        return self._prev

    def options(self, kind):
        return {}


def test_enters_only_on_the_first_day_of_the_window():
    from tradeloop.strategies.credit_spreads import CreditSpread
    thu = date(2026, 10, 29)
    # started on Wednesday for a Thursday expiry: Tuesday was the first day in the window
    late = CreditSpread(CFG)
    late.on_minute(_Ctx(date(2026, 10, 28), thu, prev=date(2026, 10, 27)))
    assert late.state == "done" and late.long is None
    # on Tuesday itself (previous day Monday, 3 days out) it goes ahead
    on_time = CreditSpread(CFG)
    on_time.on_minute(_Ctx(date(2026, 10, 27), thu, prev=date(2026, 10, 26)))
    assert on_time.state == "done" and on_time.day == date(2026, 10, 27)   # entered (no quotes, so no order)


def test_early_exit_buys_the_short_back_first_then_sells_the_protection():
    c = candles()
    late = (c["ts_ms"] >= ms(EXP, 10, 0))
    c.loc[late & (c["instrument"] == "NSE|NIFTY|IDX"), ["open", "high", "low", "close"]] = 24600.0   # < 24400 x 1.01
    c.loc[late & (c["instrument"] == SHORT), ["open", "high", "low", "close"]] = 60.0
    c.loc[late & (c["instrument"] == LONG), ["open", "high", "low", "close"]] = 20.0
    fills = []
    orig = engine_mod.Engine._on_fill
    engine_mod.Engine._on_fill = lambda self, f: (fills.append(f), orig(self, f))
    try:
        rows = run_week({**CFG, "exit_within_pct": 1.0}, "v1", NIFTY, EXP, c, 24000.0, NO_SLIP, PREV)
    finally:
        engine_mod.Engine._on_fill = orig
    assert [(str(f.instrument), f.side) for f in fills[2:]] == [(SHORT, Side.BUY), (LONG, Side.SELL)]
    assert fills[2].ts_ms == ms(EXP, 10, 1) and fills[3].ts_ms >= fills[2].ts_ms
    assert rows[-1]["settled_at_expiry"] == 0                              # flat before the close
    assert sum(r["gross"] for r in rows) == (20 - 60 + 20 - 5) * 65        # -25 points


def test_no_early_exit_unless_asked_for():
    c = candles()
    c.loc[(c["ts_ms"] >= ms(EXP, 10, 0)) & (c["instrument"] == "NSE|NIFTY|IDX"), ["close"]] = 24500.0
    rows = run_week(CFG, "v1", NIFTY, EXP, c, 25000.0, NO_SLIP, PREV)
    assert rows[-1]["settled_at_expiry"] == 2
