from datetime import date, datetime

import pandas as pd

from tradeloop.check_history import IST, check_expiry

EXP = date(2025, 6, 26)


def ms(hh, mm, d=EXP):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp() * 1000)


def frame(close=25549.0, settle_shift=0.0, extra=()):
    rows = [("NSE|NIFTY|IDX", ms(9, 15) + i * 60_000, 25500, 25510, 25490, 25500, None, None) for i in range(375)]
    for k in (25450, 25500, 25550, 25600, 25650):
        for kind in ("CE", "PE"):
            v = max(0, close - k) if kind == "CE" else max(0, k - close)
            px = max(0.05, v + settle_shift)
            rows.append((f"NSE|NIFTY|{EXP}|{k}|{kind}", ms(15, 29), px, px, px, px, 650.0, 1000.0))
    rows += list(extra)
    candles = pd.DataFrame(rows, columns=["instrument", "ts_ms", "open", "high", "low", "close", "volume", "oi"])
    daily = pd.DataFrame([("NSE|NIFTY|IDX", ms(0, 0), 25240, 25560, 25020, close, None, None)],
                         columns=candles.columns)
    return candles, daily


def test_clean_expiry_passes():
    r = check_expiry(*frame(), EXP)
    assert r["problems"] == [] and r["notes"] == [] and r["settle_max_error"] <= 0.05   # 0.05: minimum tick


def test_systematic_settlement_error_is_a_problem():
    r = check_expiry(*frame(settle_shift=20), EXP)
    assert any("official close" in p for p in r["problems"])


def test_structure_problems_and_special_session_note():
    dup = ("NSE|NIFTY|IDX", ms(9, 15), 25500, 25510, 25490, 25500, None, None)
    broken = (f"NSE|NIFTY|{EXP}|25500|CE", ms(10, 0), 50, 40, 60, 50, 65.0, None)       # low > high
    muhurat = (f"NSE|NIFTY|{EXP}|25500|CE", ms(18, 30, date(2025, 6, 21)), 50, 50, 50, 50, 65.0, None)
    r = check_expiry(*frame(extra=[dup, broken, muhurat]), EXP)
    assert "1 duplicate rows" in r["problems"] and "1 impossible candles" in r["problems"]
    assert any("special session" in n and "2025-06-21" in n for n in r["notes"])


def test_missing_official_close_and_index_gap():
    candles, _ = frame()
    candles = candles[~((candles.instrument == "NSE|NIFTY|IDX") & (candles.ts_ms == ms(12, 0)))]
    r = check_expiry(candles, None, EXP)
    assert "no official close (daily candle) for expiry day" in r["problems"]
    assert any("index missing 1 of 375" in p for p in r["problems"])
