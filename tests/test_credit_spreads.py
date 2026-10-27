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
