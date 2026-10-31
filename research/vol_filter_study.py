"""Volatility filter on the put credit spread. Declared in research/LOG.md (31 Oct 2026).

    python research/vol_filter_study.py                  # development: NIFTY 2020-2024
    python research/vol_filter_study.py --holdout        # 2025-26; run once, only for a filter that passed

Filter A: skip if ATM IV at the 09:20 entry > 20%.  Filter B: skip if the realised volatility
of the previous 10 daily closes > 20% (annualised).  Per-week net P&L is rupees per lot after
charges and slippage, from the real backtest of strategies/put_credit_spread.yaml.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from iv_rich_study import daily_closes, realised_vol
from tradeloop.backtest import load_expiry, run
from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST
from tradeloop.derive_greeks import derive

CFG = Path("strategies/put_credit_spread.yaml")
INDEX = InstrumentId.parse("NSE|NIFTY|IDX")
THRESHOLD = 20.0
BIG_LOSS = -5000
LABELS = {"A": "A  ATM IV > 20%", "B": "B  10-day realised vol > 20%"}


def entry_iv(candles: pd.DataFrame, expiry: str, day: str) -> float | None:
    """ATM implied volatility (%) at the last minute up to 09:20 on `day`, mean of CE and PE."""
    d = date.fromisoformat(day)
    lo = int(datetime.combine(d, time(9, 15), IST).timestamp() * 1000)
    hi = int(datetime.combine(d, time(9, 20), IST).timestamp() * 1000)
    sl = candles[(candles["ts_ms"] >= lo) & (candles["ts_ms"] <= hi)]
    g = derive(sl, expiry)
    g = g[g["iv"].notna() & (g["flag"] == "")]
    for ts in sorted(g["ts_ms"].unique(), reverse=True):
        m = g[g["ts_ms"] == ts].copy()
        m["strike"] = m["instrument"].str.split("|").str[3].astype(float)
        m["kind"] = m["instrument"].str.split("|").str[-1]
        m["dist"] = (m["strike"] - m["forward"]).abs()
        for _, row in m.sort_values("dist").iterrows():
            both = m[m["strike"] == row["strike"]]
            if set(both["kind"]) == {"CE", "PE"}:
                return float(both["iv"].mean()) * 100
    return None


def weeks(history: Path, start: date, end: date) -> pd.DataFrame:
    rep = run(CFG, history, start, end)
    rep = rep[rep["index"] == str(INDEX)]
    traded = rep.groupby("expiry").filter(lambda g: g["fills"].sum() > 0)
    closes = daily_closes(history, "NSE_NIFTY")
    rv = realised_vol(closes, 10)
    out = []
    for expiry, g in traded.groupby("expiry"):
        entry_day = g.loc[g["fills"] > 0, "day"].min()
        candles, _ = load_expiry(history, INDEX, date.fromisoformat(expiry))
        out.append({"expiry": expiry, "day": entry_day, "net": g["net"].sum(),
                    "iv": entry_iv(candles, expiry, entry_day), "rv": rv.get(entry_day, np.nan)})
    return pd.DataFrame(out)


def evaluate(w: pd.DataFrame) -> str:
    base_total, big = w["net"].sum(), w[w["net"] < BIG_LOSS]
    lines = [f"unfiltered: {len(w)} weeks, total Rs {base_total:,.0f}, avg Rs {w['net'].mean():,.0f}/week, "
             f"{len(big)} weeks lost > Rs {-BIG_LOSS:,}, worst Rs {w['net'].min():,.0f}, "
             f"IV known for {w['iv'].notna().sum()}, RV known for {w['rv'].notna().sum()}"]
    for key, col in (("A", "iv"), ("B", "rv")):
        skip = w[col] > THRESHOLD
        keep = w[~skip]
        skipped_big = int((skip & (w["net"] < BIG_LOSS)).sum())
        a = len(big) > 0 and skipped_big >= len(big) / 2
        b = keep["net"].sum() >= 0.9 * base_total
        c = skip.mean() <= 0.30
        lines.append(f"\nFilter {LABELS[key]}: skips {int(skip.sum())} of {len(w)} weeks ({skip.mean():.0%}); "
                     f"kept total Rs {keep['net'].sum():,.0f} ({keep['net'].sum() / base_total:.0%} of unfiltered), "
                     f"kept avg Rs {keep['net'].mean():,.0f}, kept worst Rs {keep['net'].min():,.0f}")
        lines.append(f"  big-loss weeks skipped {skipped_big} of {len(big)}; skipped weeks' total Rs {w[skip]['net'].sum():,.0f}")
        lines.append(f"  (a) skips >= half the big losses: {a}   (b) keeps >= 90% of P&L: {b}   (c) skips <= 30%: {c}"
                     f"   -> {'PASS' if a and b and c else 'FAIL'}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--holdout", action="store_true")
    a = p.parse_args()
    start, end = (date(2025, 1, 1), date(2026, 10, 27)) if a.holdout else (date(2020, 1, 1), date(2024, 12, 31))
    w = weeks(Path(a.history), start, end)
    tag = "holdout" if a.holdout else "dev"
    w.to_csv(f"data/research/vol_filter_{tag}.csv", index=False)
    print(evaluate(w))


if __name__ == "__main__":
    main()
