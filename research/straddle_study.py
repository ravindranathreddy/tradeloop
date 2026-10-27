"""Study 1: is the at-the-money straddle priced for bigger moves than happen?

    python research/straddle_study.py [--history data/history] [--out data/research]

For every trading day (each under its nearest weekly expiry):
  09:20  index level, ATM strike (nearest strike with both a call and a put quoted),
         straddle price = call + put (the market's implied move, in points)
  end    expiry day: the straddle's settlement value |official close - strike|
         other days: the straddle's price at 15:15
  seller_pts = price at 09:20 - value at the end  (a buyer gets exactly the opposite)

Prices are candle closes, so before charges and slippage (~Rs 100-150 per lot round trip
for a NIFTY straddle). Writes one row per day and prints a summary by days to expiry.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST
from tradeloop.lot_sizes import default_lot_sizes

ENTRY, EXIT = time(9, 20), time(15, 15)


def _ms(d: date, t: time) -> int:
    return int(datetime.combine(d, t, IST).timestamp() * 1000)


def _as_of(df: pd.DataFrame, ts: int) -> pd.Series:
    """Last close per instrument in the 5 minutes up to ts (the candle starting at ts-1min closes at ts)."""
    w = df[(df["ts_ms"] < ts) & (df["ts_ms"] >= ts - 5 * 60_000)]
    return w.sort_values("ts_ms").groupby("instrument")["close"].last()


def study_expiry(name: str, expiry: date, previous: date, history: Path) -> list[dict]:
    c = pd.read_parquet(history / "candles_1m" / name / f"{expiry}.parquet")
    daily = pd.read_parquet(history / "candles_1d" / name / f"{expiry}.parquet")
    dd = pd.to_datetime(daily["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date
    official = daily.loc[dd == expiry, "close"]
    official = float(official.iloc[0]) if len(official) else None
    g_path = history / "greeks_1m" / name / f"{expiry}.parquet"
    greeks = pd.read_parquet(g_path, columns=["instrument", "ts_ms", "iv"]) if g_path.exists() else None

    parts = c["instrument"].str.split("|")
    c = c.assign(kind=parts.str[-1], strike=pd.to_numeric(parts.str[3], errors="coerce"),
                 day=pd.to_datetime(c["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date)
    index_id = f"{name.replace('_', '|')}|IDX"
    lot = None
    rows = []
    for d in sorted(x for x in c["day"].unique() if previous < x <= expiry):
        day = c[c["day"] == d]
        t0 = _ms(d, ENTRY)
        at0 = _as_of(day, t0)
        if index_id not in at0:
            continue
        spot = float(at0[index_id])
        opts = day[day["kind"].isin(["CE", "PE"])]
        quoted = at0[at0.index.isin(opts["instrument"])]
        meta = opts.drop_duplicates("instrument").set_index("instrument")[["kind", "strike"]]
        q = meta.loc[quoted.index].assign(close=quoted.values)
        both = q.pivot_table(index="strike", columns="kind", values="close")
        if not {"CE", "PE"} <= set(both.columns):
            continue
        both = both.dropna()
        if both.empty:
            continue
        k = min(both.index, key=lambda s: abs(s - spot))
        price = float(both.loc[k, "CE"] + both.loc[k, "PE"])
        ce_id = f"{name.replace('_', '|')}|{expiry}|{int(k) if float(k).is_integer() else k}|CE"
        lot = lot or default_lot_sizes().lot_size(InstrumentId.parse(ce_id))

        if d == expiry:
            if official is None:
                continue
            end_value, end_spot = abs(official - k), official
        else:
            at1 = _as_of(day, _ms(d, EXIT))
            pe_id = ce_id[:-2] + "PE"
            if ce_id not in at1 or pe_id not in at1 or index_id not in at1:
                continue
            end_value, end_spot = float(at1[ce_id] + at1[pe_id]), float(at1[index_id])

        iv = None
        if greeks is not None:
            g = greeks[(greeks["instrument"].isin([ce_id, ce_id[:-2] + "PE"])) &
                       (greeks["ts_ms"] < t0) & (greeks["ts_ms"] >= t0 - 5 * 60_000)]
            iv = float(g["iv"].mean()) * 100 if g["iv"].notna().any() else None
        rows.append({"index": name, "expiry": expiry.isoformat(), "day": d.isoformat(),
                     "days_to_expiry": (expiry - d).days, "spot": spot, "strike": k,
                     "straddle": round(price, 2), "straddle_pct": round(price / spot * 100, 3),
                     "iv": iv, "end_value": round(end_value, 2),
                     "move_pct": round((end_spot - spot) / spot * 100, 3),
                     "seller_pts": round(price - end_value, 2), "lot": lot,
                     "seller_rs_per_lot": round((price - end_value) * lot, 0)})
    return rows


def run(history: Path) -> pd.DataFrame:
    manifest = pd.read_csv(history / "expiries.csv")
    rows = []
    for index_s, g in manifest.groupby("index"):
        name = "_".join(index_s.split("|")[:2])
        real = sorted(date.fromisoformat(e) for e in g.loc[g["status"] == "ok", "expiry"])
        for i, expiry in enumerate(real):
            previous = real[i - 1] if i else date.fromordinal(expiry.toordinal() - 7)
            rows += study_expiry(name, expiry, previous, history)
    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame) -> str:
    out = []
    for name, g in df.groupby("index"):
        out.append(f"\n{name}: {len(g)} days ({g['day'].min()} .. {g['day'].max()})")
        out.append(f"{'days to expiry':>14} {'days':>5} {'straddle %':>10} {'seller wins':>11} "
                   f"{'avg pts':>8} {'avg Rs/lot':>10} {'avg win':>8} {'avg loss':>9} {'worst Rs/lot':>12}")
        for dte, h in g.groupby("days_to_expiry"):
            wins, losses = h[h["seller_pts"] > 0], h[h["seller_pts"] <= 0]
            out.append(f"{dte:>14} {len(h):>5} {h['straddle_pct'].median():>9.2f}% {len(wins) / len(h):>10.0%} "
                       f"{h['seller_pts'].mean():>8.1f} {h['seller_rs_per_lot'].mean():>10,.0f} "
                       f"{wins['seller_rs_per_lot'].mean():>8,.0f} {losses['seller_rs_per_lot'].mean():>9,.0f} "
                       f"{h['seller_rs_per_lot'].min():>12,.0f}")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--out", default="data/research")
    a = p.parse_args()
    df = run(Path(a.history))
    Path(a.out).mkdir(parents=True, exist_ok=True)
    df.to_csv(Path(a.out) / "straddle_study.csv", index=False)
    print(summarise(df))


if __name__ == "__main__":
    main()
