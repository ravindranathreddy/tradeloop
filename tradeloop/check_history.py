"""Quality check for downloaded expiry candles. Read-only; run any time.

    python -m tradeloop.check_history [--history data/history] [--csv report.csv]

Problems are data errors; notes are real market quirks worth knowing but expected:
special sessions (Muhurat trading,
e.g. Sat 14 Nov 2020 18:15-19:15; the NSE outage extension on 24 Feb 2021,
15:45-17:00), and a single stray last trade far from the settlement value.

Per expiry file it checks:
  structure   duplicate (instrument, minute) rows, impossible candles (low > open/close/high),
              prices <= 0 or missing, rows outside the expiry week; rows outside 09:00-16:00
              are noted, rows 15:31-16:00 only counted (post-close prints are routine)
  gaps        index minutes missing between 09:15 and 15:29 on expiry day
  settlement  the official close (daily candle) exists, and near-the-money options' last
              price on expiry day is within a few points of their value at that close
  coverage    option contracts, how many traded on expiry day, share of rows with OI

Settlement is the strongest check: a wrong strike, timestamp or file mix-up shows up
as options settling far from (official close - strike).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from tradeloop.core.market_calendar import default_calendar

IST = ZoneInfo("Asia/Kolkata")
SETTLE_TOLERANCE = 5.0           # points; last trades happen a little before 15:30
SETTLE_STRIKES = 2               # check strikes within this many steps of the close


def _ms(d: date, hh: int, mm: int) -> int:
    return int(datetime.combine(d, time(hh, mm), IST).timestamp() * 1000)


def _close_ms(d: date) -> int:
    c = default_calendar().close_time(d)
    return _ms(d, c.hour, c.minute)


def check_expiry(candles: pd.DataFrame, daily: pd.DataFrame | None, expiry: date) -> dict:
    r: dict = {"expiry": expiry.isoformat(), "rows": len(candles), "problems": [], "notes": []}
    p, n = r["problems"], r["notes"]
    parts = candles["instrument"].str.split("|")
    kind = parts.str[-1]
    opts = candles[kind.isin(["CE", "PE"])].assign(strike=pd.to_numeric(parts.str[3], errors="coerce"),
                                                   kind=kind)
    index = candles[kind == "IDX"]

    # structure
    dups = candles.duplicated(["instrument", "ts_ms"]).sum()
    if dups:
        p.append(f"{dups} duplicate rows")
    bad = ((candles["low"] > candles[["open", "close", "high"]].min(axis=1) + 1e-9) |
           (candles["high"] < candles[["open", "close", "low"]].max(axis=1) - 1e-9)).sum()
    if bad:
        p.append(f"{bad} impossible candles")
    nonpos = (~(candles[["open", "high", "low", "close"]] > 0)).any(axis=1).sum()
    if nonpos:
        p.append(f"{nonpos} rows with price <= 0 or missing")
    t = pd.to_datetime(candles["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST)
    hhmm = t.dt.strftime("%H:%M")
    r["post_close_rows"] = int(hhmm.between("15:31", "16:00").sum())    # routine, counted not noted
    outside = ~hhmm.between("09:00", "16:00")
    if outside.any():
        days = sorted({str(d) for d in t[outside].dt.date})
        n.append(f"special session: {outside.sum()} rows outside 09:00-16:00 on {', '.join(days)} "
                 f"({hhmm[outside].min()}-{hhmm[outside].max()})")
    start = _ms(expiry - timedelta(days=6), 0, 0)
    out_of_week = ((candles["ts_ms"] < start) | (candles["ts_ms"] > _close_ms(expiry))).sum()
    if out_of_week:
        p.append(f"{out_of_week} rows outside the expiry week")

    # gaps in the index on expiry day
    day_lo, day_hi = _ms(expiry, 9, 15), _ms(expiry, 15, 29)
    idx_minutes = set(index.loc[index["ts_ms"].between(day_lo, day_hi), "ts_ms"])
    expected = (day_hi - day_lo) // 60_000 + 1
    r["index_minutes"] = len(idx_minutes)
    if len(idx_minutes) < expected:
        p.append(f"index missing {expected - len(idx_minutes)} of {expected} minutes on expiry day")

    # coverage
    on_day = opts[opts["ts_ms"].between(_ms(expiry, 9, 15), _close_ms(expiry))]
    r["contracts"] = opts["instrument"].nunique()
    r["traded_on_expiry"] = on_day.loc[on_day["volume"].fillna(0) > 0, "instrument"].nunique()
    r["oi_share"] = round(float(opts["oi"].notna().mean()), 2) if len(opts) else 0.0
    if r["contracts"] == 0:
        p.append("no option contracts")

    # settlement against the official close
    close = None
    if daily is not None and not daily.empty:
        dd = pd.to_datetime(daily["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date
        on = daily.loc[dd == expiry, "close"]
        close = float(on.iloc[0]) if len(on) else None
    r["official_close"] = close
    if close is None:
        p.append("no official close (daily candle) for expiry day")
    elif len(on_day):
        strikes = sorted(on_day["strike"].unique())
        step = pd.Series(strikes).diff().mode().iloc[0] if len(strikes) > 1 else 0
        near = on_day[(on_day["strike"] - close).abs() <= SETTLE_STRIKES * step]
        last = near.sort_values("ts_ms").groupby("instrument").tail(1)
        value = (close - last["strike"]).clip(lower=0).where(last["kind"] == "CE",
                                                            (last["strike"] - close).clip(lower=0))
        err = (last["close"] - value).abs()
        r["settle_max_error"] = round(float(err.max()), 2) if len(err) else None
        off = last[err > SETTLE_TOLERANCE]
        msg = (f"{len(off)} near-the-money options' last trade > {SETTLE_TOLERANCE:g} pts from "
               f"their value at the official close (worst {err.max():.1f})")
        if len(off) > 1:                  # systematic: wrong strikes, times or file
            p.append(msg)
        elif len(off) == 1:               # one stray last print happens in real markets
            n.append(msg)
    return r


def run(history: Path) -> pd.DataFrame:
    rows = []
    for src in sorted((history / "candles_1m").glob("*/*.parquet")):
        daily_path = history / "candles_1d" / src.parent.name / src.name
        daily = pd.read_parquet(daily_path) if daily_path.exists() else None
        r = check_expiry(pd.read_parquet(src), daily, date.fromisoformat(src.stem))
        r["index"] = src.parent.name
        r["problems"] = "; ".join(r["problems"])
        r["notes"] = "; ".join(r["notes"])
        rows.append(r)
    return pd.DataFrame(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--csv", help="also write the full report here")
    a = p.parse_args()
    report = run(Path(a.history))
    if report.empty:
        print("No expiry files found")
        return
    if a.csv:
        report.to_csv(a.csv, index=False)
    for name, g in report.groupby("index"):
        bad = g[g["problems"] != ""]
        print(f"{name}: {len(g)} expiries ({g['expiry'].min()} .. {g['expiry'].max()}), "
              f"{len(bad)} with problems")
        print(f"  contracts per expiry median {g['contracts'].median():.0f}, traded on expiry day "
              f"median {g['traded_on_expiry'].median():.0f}, OI on {g['oi_share'].median():.0%} of rows")
        print(f"  settlement error (worst near-the-money option) median "
              f"{g['settle_max_error'].median():.2f} pts, max {g['settle_max_error'].max():.2f}")
        late = g[g["post_close_rows"] > 0]
        if len(late):
            print(f"  rows between 15:31 and 16:00 in {len(late)} expiries ({late['expiry'].min()} .. "
                  f"{late['expiry'].max()}), {late['post_close_rows'].sum()} rows")
        for _, r in bad.iterrows():
            print(f"  PROBLEM {r['expiry']}: {r['problems']}")
        for _, r in g[g["notes"] != ""].iterrows():
            print(f"  note    {r['expiry']}: {r['notes']}")
    sys.exit(1 if (report["problems"] != "").any() else 0)


if __name__ == "__main__":
    main()
