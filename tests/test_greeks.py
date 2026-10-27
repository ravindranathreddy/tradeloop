from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from tradeloop.derive_greeks import IST, MS_PER_YEAR, derive, expiry_ms, synthetic_forward, split_instruments
from tradeloop.greeks import black76, greeks, implied_vol

F, T = 25000.0, 2 / 365


# --- model ---------------------------------------------------------------------------

@pytest.mark.parametrize("sigma", [0.08, 0.15, 0.4, 1.5])
def test_iv_recovers_the_volatility_that_made_the_price(sigma):
    K = np.array([24500.0, 24900, 25000, 25100, 25500])
    for is_call in (True, False):
        price = black76(F, K, T, sigma, is_call)
        assert np.allclose(implied_vol(price, F, K, T, is_call), sigma, atol=1e-6)


def test_put_call_parity():
    K, r = 25100.0, 0.065
    c, p = black76(F, K, T, 0.15, True, r), black76(F, K, T, 0.15, False, r)
    assert np.isclose(c - p, np.exp(-r * T) * (F - K))


def test_impossible_prices_give_nan():
    K = np.array([24800.0, 25000.0, 25000.0])
    price = np.array([150.0, 30000.0, 100.0])         # below intrinsic (200), above forward, fine
    iv = implied_vol(price, F, K, T, True)
    assert np.isnan(iv[0]) and np.isnan(iv[1]) and not np.isnan(iv[2])
    assert np.isnan(implied_vol(10.0, F, 25000.0, 0.0, True))      # expired


def test_greeks_match_finite_differences():
    K, sigma, r, h = 25100.0, 0.15, 0.065, 1.0
    for is_call in (True, False):
        g = greeks(F, K, T, sigma, is_call, r)
        spot = F * np.exp(-r * T)
        price = lambda s, v=sigma, t=T: black76(s * np.exp(r * t), K, t, v, is_call, r)
        assert np.isclose(g["delta"], (price(spot + h) - price(spot - h)) / (2 * h), rtol=1e-4)
        assert np.isclose(g["gamma"], (price(spot + h) - 2 * price(spot) + price(spot - h)) / h ** 2, rtol=1e-3)
        assert np.isclose(g["vega"], (price(spot, sigma + 0.0001) - price(spot, sigma - 0.0001)) / 0.02, rtol=1e-4)
        dt = 1 / (365 * 24 * 60)                     # one minute, scaled to a day: theta is a rate
        assert np.isclose(g["theta"], (price(spot, sigma, T - dt) - price(spot)) / dt / 365, rtol=1e-3)


def test_delta_signs():
    g = greeks(F, np.array([24000.0, 26000.0]), T, 0.15, np.array([True, False]))
    assert 0.9 < g["delta"][0] <= 1 and -1 <= g["delta"][1] < -0.9


# --- deriving from candles ------------------------------------------------------------

EXP = "2025-06-26"


def ms(hh, mm, day=26):
    return int(datetime(2025, 6, day, hh, mm, tzinfo=IST).timestamp() * 1000)


def candles(rows):
    return pd.DataFrame(rows, columns=["instrument", "ts_ms", "open", "high", "low", "close", "volume", "oi"])


def opt_row(strike, kind, ts, close):
    return (f"NSE|NIFTY|{EXP}|{strike}|{kind}", ts, close, close, close, close, 650.0, None)


def idx_row(ts, close):
    return ("NSE|NIFTY|IDX", ts, close, close, close, close, None, None)


def priced(strike, kind, ts, fwd, sigma):
    t = (expiry_ms(EXP) - ts) / MS_PER_YEAR
    return round(float(black76(fwd, strike, t, sigma, kind == "CE")), 2)


def test_synthetic_forward_uses_the_pair_nearest_the_index():
    ts = ms(10, 0)
    df = split_instruments(candles([
        idx_row(ts, 25040.0),
        opt_row(25000, "CE", ts, 120.0), opt_row(25000, "PE", ts, 70.0),
        opt_row(25100, "CE", ts, 60.0), opt_row(25100, "PE", ts, 115.0),
    ]))
    fwd = synthetic_forward(df[df.kind.isin(["CE", "PE"])], df[df.kind == "IDX"], expiry_ms(EXP))
    t = (expiry_ms(EXP) - ts) / MS_PER_YEAR
    assert np.isclose(fwd["forward"].iloc[0], 25000 + 50 * np.exp(0.065 * t))   # 25000 is nearer 25040


def test_derive_recovers_iv_and_flags_bad_rows():
    fwd, sigma = 25049.0, 0.12
    t1, t2, t3 = ms(10, 0), ms(10, 3), ms(15, 30)
    rows = [idx_row(t1, 25030.0), idx_row(t2, 25031.0)]
    for k in (25000, 25100):
        for kind in ("CE", "PE"):
            rows.append(opt_row(k, kind, t1, priced(k, kind, t1, fwd, sigma)))
    rows.append(opt_row(25100, "CE", t2, priced(25100, "CE", t2, fwd, sigma)))   # alone: forward carried
    rows.append(opt_row(24500, "CE", t2, 300.0))                                   # below intrinsic (~549)
    rows.append(opt_row(26000, "CE", t2, 0.05))                                    # at the minimum tick
    rows.append(opt_row(25000, "CE", t3, 49.0))                                    # 15:30 print
    out = derive(candles(rows), EXP).set_index(["instrument", "ts_ms"])

    clean = out[out["flag"] == ""]
    assert len(clean) == 4 and np.allclose(clean["iv"], sigma, atol=2e-3)
    assert np.allclose(clean["forward"], fwd, atol=0.2)
    assert out.loc[(f"NSE|NIFTY|{EXP}|25100|CE", t2), "flag"] == "fwd_carried"
    assert abs(out.loc[(f"NSE|NIFTY|{EXP}|25100|CE", t2), "iv"] - sigma) < 2e-3
    assert out.loc[(f"NSE|NIFTY|{EXP}|24500|CE", t2), "flag"] == "fwd_carried|no_iv"
    assert "min_price" in out.loc[(f"NSE|NIFTY|{EXP}|26000|CE", t2), "flag"]
    assert "near_expiry" in out.loc[(f"NSE|NIFTY|{EXP}|25000|CE", t3), "flag"]
    assert np.isnan(out.loc[(f"NSE|NIFTY|{EXP}|24500|CE", t2), "delta"])


def test_no_pair_means_no_forward():
    ts = ms(10, 0)
    out = derive(candles([idx_row(ts, 25000.0), opt_row(25000, "CE", ts, 100.0)]), EXP)
    assert out["flag"].tolist() == ["no_forward"] and out["iv"].isna().all()
