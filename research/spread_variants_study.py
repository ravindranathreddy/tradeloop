"""Two ways to cut the put credit spread's big losses without skipping weeks.
Declared in research/LOG.md (31 Oct 2026, "two ways to cut big losses").

    python research/spread_variants_study.py              # development: NIFTY 2020-2024
    python research/spread_variants_study.py --holdout    # 2025-26; once, only for a variant that passed

Test 1  strike distance = k x expected move, clamped to 2.5-4.0%, protection 2.0% further.
Test 2  exit_within_pct 1.0: close the spread when the index is within 1% of the sold strike.
Base weeks and ATM IV at entry come from data/research/vol_filter_{dev,holdout}.csv
(research/vol_filter_study.py). Rs per lot, after charges and slippage.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from tradeloop.backtest import config_version, exclusions, load_expiry, run_week
from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST, default_calendar
from tradeloop.fill_model import FillModel

CFG = Path("strategies/put_credit_spread.yaml")
INDEX = InstrumentId.parse("NSE|NIFTY|IDX")
BIG_LOSS = -5000
SHORT_MIN, SHORT_MAX, WIDTH = 2.5, 4.0, 2.0


def years_to_expiry(day: str, expiry: str) -> float:
    d, e = date.fromisoformat(day), date.fromisoformat(expiry)
    entry = datetime.combine(d, time(9, 20), IST)
    close = datetime.combine(e, default_calendar().close_time(e), IST)
    return (close - entry).total_seconds() / (365 * 86400)


def expected_move_pct(w: pd.DataFrame) -> pd.Series:
    t = np.array([years_to_expiry(d, e) for d, e in zip(w["day"], w["expiry"])])
    return w["iv"] * np.sqrt(t)


def run_weeks(history: Path, expiries: list[str], cfg_for) -> dict[str, float]:
    base = yaml.safe_load(CFG.read_text())
    version, skip = config_version(CFG), exclusions()
    manifest = pd.read_csv(history / "expiries.csv")
    mine = manifest[manifest["index"] == str(INDEX)]
    real = sorted(date.fromisoformat(e) for e in mine.loc[mine["status"].isin(["ok", "failed"]), "expiry"])
    out = {}
    for exp_s in expiries:
        expiry = date.fromisoformat(exp_s)
        candles, official = load_expiry(history, INDEX, expiry)
        earlier = [e for e in real if e < expiry]
        previous = earlier[-1] if earlier else expiry - timedelta(days=7)
        rows = run_week(cfg_for(base, exp_s), version, INDEX, expiry, candles, official, FillModel(), previous)
        out[exp_s] = round(sum(r["net"] for r in rows), 2)
    return out


def judge(name: str, w: pd.DataFrame, col: str, base_total_floor: float | None = None) -> str:
    big = w["base"] < BIG_LOSS
    base_big, new_big = w.loc[big, "base"].sum(), w.loc[big, col].sum()
    shrink = 1 - new_big / base_big if base_big else float("nan")
    keep = w[col].sum() / w["base"].sum()
    a, b, c = shrink >= 0.40, keep >= 0.90, w[col].min() >= w["base"].min()
    changed = (w[col] - w["base"]).abs() > 0.5
    hurt = (w[col] < w["base"] - 0.5)
    lines = [f"\n{name}",
             f"  total Rs {w[col].sum():,.0f} vs base Rs {w['base'].sum():,.0f} ({keep:.0%}); worst week Rs {w[col].min():,.0f} "
             f"vs Rs {w['base'].min():,.0f}",
             f"  big-loss weeks ({int(big.sum())}): Rs {base_big:,.0f} -> Rs {new_big:,.0f} (shrinks {shrink:.0%})",
             f"  weeks changed {int(changed.sum())}, weeks it made worse {int(hurt.sum())} (cost Rs {(w.loc[hurt, 'base'] - w.loc[hurt, col]).sum():,.0f}), "
             f"weeks better {int((w[col] > w['base'] + 0.5).sum())} (gain Rs {(w.loc[w[col] > w['base'] + 0.5, col] - w.loc[w[col] > w['base'] + 0.5, 'base']).sum():,.0f})",
             f"  (a) big losses shrink >= 40%: {a}   (b) keeps >= 90% of P&L: {b}   (c) worst week no worse: {c}"
             f"   -> {'PASS' if a and b and c else 'FAIL'}"]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--holdout", action="store_true")
    a = p.parse_args()
    tag = "holdout" if a.holdout else "dev"
    w = pd.read_csv(f"data/research/vol_filter_{tag}.csv").rename(columns={"net": "base"})
    w["em"] = expected_move_pct(w)
    dev = pd.read_csv("data/research/vol_filter_dev.csv")
    k = float(np.median(SHORT_MIN / expected_move_pct(dev)))        # from entry data alone, development years
    w["short_pct"] = np.clip(k * w["em"], SHORT_MIN, SHORT_MAX)
    print(f"k = {k:.3f} (median of 2.5 / expected move, development); short distance: "
          f"{(w['short_pct'] > SHORT_MIN).mean():.0%} of weeks moved out, median {w['short_pct'].median():.2f}%, max {w['short_pct'].max():.2f}%")
    moved = dict(zip(w["expiry"], w["short_pct"]))
    h = Path(a.history)
    t1 = run_weeks(h, list(w["expiry"]), lambda cfg, e: {**cfg, "short_otm_pct": float(moved[e]),
                                                         "long_otm_pct": float(moved[e]) + WIDTH})
    t2 = run_weeks(h, list(w["expiry"]), lambda cfg, e: {**cfg, "exit_within_pct": 1.0})
    w["t1"], w["t2"] = w["expiry"].map(t1), w["expiry"].map(t2)
    w.to_csv(f"data/research/spread_variants_{tag}.csv", index=False)
    print(f"base reproduces the backtest: {w['base'].sum():,.0f} total, {len(w)} weeks")
    print(judge("Test 1: strike = k x expected move", w, "t1"))
    print(judge("Test 2: early exit within 1% of the sold strike", w, "t2"))


if __name__ == "__main__":
    main()
