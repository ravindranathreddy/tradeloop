"""Study 4: does OI build-up predict where the index settles on expiry day?

    python research/oi_walls_study.py

The Sensibull reading: put OI added below the price is "support", call OI added above it
is "resistance", so the index should settle between them. Expiry days, development years
2020-2024 only (2025-2026 held out).

At a check time T, among strikes within 10 steps of the index:
  put wall   strike at or below the index with the largest put OI (change or total)
  call wall  strike at or above the index with the largest call OI
  inside     the official close lands within [put wall, call wall]
  control    a band of the same width centred on the index at T

If OI carries no information, the OI band and the control catch the close equally often.

OI change = OI at T minus the previous trading day's last OI (as Sensibull's intraday
view). Variants, declared before running: {change, total} x T {10:30, 12:30} x 2 indices
= 8 cells.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from tradeloop.core.market_calendar import IST

DEV_END = date(2024, 12, 31)
STEP = {"NIFTY": 50, "SENSEX": 100}
TIMES = [time(10, 30), time(12, 30)]
WINDOW = 10


def _ms(d: date, t: time) -> int:
    return int(datetime.combine(d, t, IST).timestamp() * 1000)


def last_oi(df: pd.DataFrame, before_ms: int) -> pd.Series:
    w = df[(df["ts_ms"] < before_ms) & df["oi"].notna()]
    return w.sort_values("ts_ms").groupby("instrument")["oi"].last()


def study_expiry(name: str, expiry: date, history: Path) -> list[dict]:
    exchange, underlying = name.split("_")
    step = STEP[underlying]
    c = pd.read_parquet(history / "candles_1m" / name / f"{expiry}.parquet")
    daily = pd.read_parquet(history / "candles_1d" / name / f"{expiry}.parquet")
    dd = pd.to_datetime(daily["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date
    close = daily.loc[dd == expiry, "close"]
    if close.empty:
        return []
    close = float(close.iloc[0])
    index_id = f"{exchange}|{underlying}|IDX"
    opts = c[c["instrument"].str.endswith(("CE", "PE"))]
    parts = opts["instrument"].str.split("|")
    meta = pd.DataFrame({"strike": parts.str[3].astype(float).values, "kind": parts.str[-1].values},
                        index=opts["instrument"].values)
    meta = meta[~meta.index.duplicated()]
    prev_close_oi = last_oi(opts, _ms(expiry, time(9, 0)))            # previous days in the file
    rows = []
    for t in TIMES:
        t_ms = _ms(expiry, t)
        idx = c[(c["instrument"] == index_id) & (c["ts_ms"] < t_ms) & (c["ts_ms"] >= t_ms - 5 * 60_000)]
        if idx.empty:
            continue
        spot = float(idx.sort_values("ts_ms")["close"].iloc[-1])
        now = last_oi(opts[opts["ts_ms"] >= _ms(expiry, time(9, 0))], t_ms)
        for measure in ("change", "total"):
            val = now - prev_close_oi.reindex(now.index).fillna(0) if measure == "change" else now
            m = meta.loc[val.index].assign(val=val.values)
            m = m[(m["strike"] - spot).abs() <= WINDOW * step]
            puts = m[(m["kind"] == "PE") & (m["strike"] <= spot)]
            calls = m[(m["kind"] == "CE") & (m["strike"] >= spot)]
            if puts.empty or calls.empty:
                continue
            put_wall = float(puts.loc[puts["val"].idxmax(), "strike"])
            call_wall = float(calls.loc[calls["val"].idxmax(), "strike"])
            width = call_wall - put_wall
            rows.append({"index": name, "day": expiry.isoformat(), "time": t.strftime("%H:%M"), "measure": measure,
                         "spot": spot, "put_wall": put_wall, "call_wall": call_wall, "close": close,
                         "width_pct": width / spot * 100,
                         "inside": put_wall <= close <= call_wall,
                         "control_inside": abs(close - spot) <= width / 2,
                         "dist_mid": abs(close - (put_wall + call_wall) / 2), "dist_spot": abs(close - spot)})
    return rows


def run(history: Path) -> pd.DataFrame:
    manifest = pd.read_csv(history / "expiries.csv")
    ok = manifest[manifest["status"] == "ok"]
    rows = []
    for index_s, g in ok.groupby("index"):
        name = "_".join(index_s.split("|")[:2])
        for e in sorted(g["expiry"]):
            expiry = date.fromisoformat(e)
            if expiry <= DEV_END:
                rows += study_expiry(name, expiry, history)
    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame) -> str:
    out = [f"{'index':>10} {'time':>5} {'measure':>7} {'days':>4} {'band width':>10} {'OI band':>7} "
           f"{'control':>7} {'difference':>10} {'95% range':>16} {'closer to OI mid':>16}"]
    for (name, t, m), h in df.groupby(["index", "time", "measure"]):
        diff = h["inside"].astype(float) - h["control_inside"].astype(float)
        se = diff.std() / np.sqrt(len(diff))
        out.append(f"{name:>10} {t:>5} {m:>7} {len(h):>4} {h['width_pct'].median():>9.2f}% "
                   f"{h['inside'].mean():>7.0%} {h['control_inside'].mean():>7.0%} {diff.mean() * 100:>+9.0f}pt "
                   f"{(diff.mean() - 1.96 * se) * 100:>+6.0f} .. {(diff.mean() + 1.96 * se) * 100:>+4.0f}pt "
                   f"{(h['dist_mid'] < h['dist_spot']).mean():>15.0%}")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--out", default="data/research/oi_walls_study.csv")
    a = p.parse_args()
    df = run(Path(a.history))
    df.to_csv(a.out, index=False)
    print(summarise(df))


if __name__ == "__main__":
    main()
