"""Study 5: which options are mispriced? Price paid vs what the option paid out at expiry.

    python research/mispricing_study.py

For every trading day (development years 2020-2024; 2025-2026 held out), each option
quoted at 09:20 within 3% of the index: buy at the 09:20 price, hold to expiry, receive
its settlement value (intrinsic value at the official close). Grouped by:
  distance   how far out of the money, as % of the index (negative = in the money)
  type       call or put
  days       days to expiry (0, 1-2, 3+)

payout ratio = total paid out / total premium paid. 1.00 is fair; below 1, buyers overpay
(the seller's side has the better risk/reward); above 1, buyers are underpaying.

Days in the same week share one settlement, so the 95% ranges are computed over expiry
weeks (each week's own payout ratio), not over days. Before costs; the cost per rupee of
premium is shown separately because it is large for cheap options.

Variants, declared before running: 6 distance buckets x 2 types x 3 day groups x 2 indices.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from tradeloop.core.market_calendar import IST

DEV_END = date(2024, 12, 31)
ENTRY = time(9, 20)
DIST_CUTS = [-3.0, -0.25, 0.25, 0.5, 1.0, 2.0, 3.0]
DIST_LABELS = ["in the money", "at the money", "0.25-0.5% out", "0.5-1% out", "1-2% out", "2-3% out"]
COST_PER_LOT = 60          # brokerage Rs 20 + charges + 2 ticks slippage on a buy, roughly
LOT = {"NIFTY": 65, "SENSEX": 20}  # only for the cost column; the ratio itself is per unit


def study_expiry(name: str, expiry: date, previous: date, history: Path) -> list[dict]:
    exchange, underlying = name.split("_")
    c = pd.read_parquet(history / "candles_1m" / name / f"{expiry}.parquet")
    daily = pd.read_parquet(history / "candles_1d" / name / f"{expiry}.parquet")
    dd = pd.to_datetime(daily["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date
    settle = daily.loc[dd == expiry, "close"]
    if settle.empty:
        return []
    settle = float(settle.iloc[0])
    c["day"] = pd.to_datetime(c["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date
    index_id = f"{exchange}|{underlying}|IDX"
    rows = []
    for d in sorted(x for x in c["day"].unique() if previous < x <= min(expiry, DEV_END)):
        t0 = int(datetime.combine(d, ENTRY, IST).timestamp() * 1000)
        w = c[(c["day"] == d) & (c["ts_ms"] < t0) & (c["ts_ms"] >= t0 - 5 * 60_000)]
        last = w.sort_values("ts_ms").groupby("instrument")["close"].last()
        if index_id not in last:
            continue
        spot = float(last[index_id])
        opts = last[last.index.str.endswith(("CE", "PE"))]
        parts = opts.index.str.split("|")
        strike = parts.str[3].astype(float)
        kind = parts.str[-1]
        call = kind == "CE"
        dist = np.where(call, (strike - spot), (spot - strike)) / spot * 100
        payoff = np.where(call, np.maximum(settle - strike, 0), np.maximum(strike - settle, 0))
        keep = (np.abs(dist) <= 3.0) & (opts.values > 0)
        for i in np.flatnonzero(keep):
            rows.append({"index": name, "expiry": expiry.isoformat(), "day": d.isoformat(),
                         "dte": (expiry - d).days, "type": "call" if call[i] else "put",
                         "dist_pct": round(float(dist[i]), 3), "price": float(opts.values[i]),
                         "payoff": float(payoff[i])})
    return rows


def run(history: Path) -> pd.DataFrame:
    manifest = pd.read_csv(history / "expiries.csv")
    rows = []
    for index_s, g in manifest.groupby("index"):
        name = "_".join(index_s.split("|")[:2])
        real = sorted(date.fromisoformat(e) for e in g.loc[g["status"] == "ok", "expiry"])
        for i, expiry in enumerate(real):
            previous = real[i - 1] if i else date.fromordinal(expiry.toordinal() - 7)
            if previous >= DEV_END:
                break
            rows += study_expiry(name, expiry, previous, history)
    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame) -> str:
    df = df.assign(bucket=pd.cut(df["dist_pct"], DIST_CUTS, labels=DIST_LABELS, include_lowest=True),
                   days=pd.cut(df["dte"], [-1, 0, 2, 99], labels=["expiry day", "1-2 days", "3+ days"]))
    out = []
    for (name, days), g in df.groupby(["index", "days"], observed=True):
        out.append(f"\n{name}, {days}")
        out.append(f"{'distance':>14} {'type':>5} {'options':>7} {'avg price':>9} {'payout ratio':>12} "
                   f"{'95% range':>13} {'expire worthless':>16} {'cost / premium':>14} {'seller Rs/lot net':>17}")
        lot = LOT[name.split("_")[1]]
        for (bucket, typ), h in g.groupby(["bucket", "type"], observed=True):
            weekly = h.groupby("expiry").apply(lambda x: x["payoff"].sum() / x["price"].sum(), include_groups=False)
            ratio = h["payoff"].sum() / h["price"].sum()
            se = weekly.std() / np.sqrt(len(weekly))
            out.append(f"{bucket:>14} {typ:>5} {len(h):>7} {h['price'].mean():>9.1f} {ratio:>12.2f} "
                       f"{ratio - 1.96 * se:>6.2f}..{ratio + 1.96 * se:<5.2f} {(h['payoff'] == 0).mean():>16.0%} "
                       f"{COST_PER_LOT / (h['price'].mean() * lot):>14.0%} "
                       f"{(h['price'] - h['payoff']).mean() * lot - COST_PER_LOT:>17,.0f}")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--out", default="data/research/mispricing_study.csv")
    a = p.parse_args()
    df = run(Path(a.history))
    df.to_csv(a.out, index=False)
    print(summarise(df))


if __name__ == "__main__":
    main()
