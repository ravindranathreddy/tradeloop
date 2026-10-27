"""Study 2: does selling the straddle work better when options are expensive?

    python research/iv_rich_study.py        (needs data/research/straddle_study.csv from study 1)

Two yardsticks for "expensive", both known before 09:20:
  1. IV at 09:20 (ATM, ours) / realised volatility of the previous 10 daily closes.
     Fails on expiry day: final-morning IV is nearly always high, so almost every expiry
     day lands in the top bucket.
  2. Expiry day: straddle price as % of the index / the average absolute 09:20-to-close
     move of the previous 10 trading days. Like for like: both are "expected move to the
     close". Above 1, the straddle prices a bigger move than recent days made.

Development years only (2020-2024): 2025-2026 is held out to judge a rule once, later.
Buckets use fixed ratio cut-offs, not quantiles, so nothing is fitted to the data here.
Before charges; a NIFTY/SENSEX straddle round trip costs roughly Rs 150-250 per lot.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from tradeloop.core.market_calendar import IST

DEV_END = "2024-12-31"
CUTS = [0, 0.8, 1.0, 1.2, 1.5, np.inf]
LABELS = ["< 0.8 (cheap)", "0.8-1.0", "1.0-1.2", "1.2-1.5", "> 1.5 (expensive)"]
COST_PER_LOT = 200


def daily_closes(history: Path, name: str) -> pd.Series:
    """Official daily closes, from every expiry's daily candles (they tile the calendar)."""
    frames = [pd.read_parquet(p) for p in sorted((history / "candles_1d" / name).glob("*.parquet"))]
    d = pd.concat(frames).drop_duplicates("ts_ms")
    d["day"] = pd.to_datetime(d["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date.astype(str)
    return d.groupby("day")["close"].last().sort_index()


def realised_vol(closes: pd.Series, days: int = 10) -> pd.Series:
    """Annualised % volatility of the `days` returns before each day (excludes that day)."""
    r = np.log(closes).diff()
    return (r.rolling(days).std() * np.sqrt(252) * 100).shift(1)


RATIO2_CUTS = [0, 0.7, 0.9, 1.1, 1.4, np.inf]
RATIO2_LABELS = ["< 0.7 (cheap)", "0.7-0.9", "0.9-1.1", "1.1-1.4", "> 1.4 (expensive)"]


def recent_intraday_move(s1: pd.DataFrame, closes: pd.Series, days: int = 10) -> pd.Series:
    """Average |09:20 -> close| move in % over the previous `days` trading days, by day."""
    spot = s1.drop_duplicates("day").set_index("day")["spot"].sort_index()
    move = ((closes.reindex(spot.index) - spot).abs() / spot * 100)
    return move.rolling(days).mean().shift(1)


def table(g: pd.DataFrame) -> str:
    lines = [f"{'IV / recent vol':>18} {'days':>5} {'seller wins':>11} {'avg Rs/lot':>10} "
             f"{'after cost':>10} {'avg loss':>9} {'worst':>8} {'move > 2x':>9}"]
    for label, h in g.groupby("bucket", observed=True):
        losses = h[h["seller_pts"] <= 0]
        lines.append(f"{label:>18} {len(h):>5} {(h['seller_pts'] > 0).mean():>10.0%} "
                     f"{h['seller_rs_per_lot'].mean():>10,.0f} {h['seller_rs_per_lot'].mean() - COST_PER_LOT:>10,.0f} "
                     f"{losses['seller_rs_per_lot'].mean():>9,.0f} {h['seller_rs_per_lot'].min():>8,.0f} "
                     f"{(h['end_value'] > 2 * h['straddle']).mean():>9.0%}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--study1", default="data/research/straddle_study.csv")
    p.add_argument("--out", default="data/research/iv_rich_study.csv")
    a = p.parse_args()
    s = pd.read_csv(a.study1)
    s = s[s["day"] <= DEV_END].copy()
    parts = []
    for name, g in s.groupby("index"):
        closes = daily_closes(Path(a.history), name)
        rv = realised_vol(closes)
        recent = recent_intraday_move(g, closes)
        parts.append(g.assign(rv10=g["day"].map(rv), recent_move=g["day"].map(recent)))
    s = pd.concat(parts)
    s["ratio"] = s["iv"] / s["rv10"]
    s["ratio2"] = s["straddle_pct"] / s["recent_move"]
    s.to_csv(a.out, index=False)

    for name, g in s.groupby("index"):
        print(f"\n{name}, {g['day'].min()} .. {g['day'].max()} (development years only)")
        g1 = g.dropna(subset=["ratio"]).assign(bucket=lambda x: pd.cut(x["ratio"], CUTS, labels=LABELS))
        e = g1[g1["days_to_expiry"] == 0]
        print(f"Yardstick 1, expiry day ({len(e)} days):")
        print(table(e))
        o = g1[g1["days_to_expiry"] > 0]
        print(f"Yardstick 1, other days to 15:15 ({len(o)} days):")
        print(table(o))
        e2 = g[(g["days_to_expiry"] == 0)].dropna(subset=["ratio2"])
        e2 = e2.assign(bucket=pd.cut(e2["ratio2"], RATIO2_CUTS, labels=RATIO2_LABELS))
        print(f"Yardstick 2 (straddle / recent 09:20-to-close move), expiry day ({len(e2)} days):")
        print(table(e2).replace("IV / recent vol", "straddle / recent"))


if __name__ == "__main__":
    main()
