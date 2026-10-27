"""Study 3: opening range breakout with bought options (limited risk).

    python research/orb_study.py

Per trading day (development years 2020-2024 only; 2025-2026 held out):
  opening range  index high/low over the first N minutes (09:15 to 09:15+N)
  signal         first index close above the high (long) or below the low (short),
                 between the end of the range and 12:00
  trade          buy the ATM option in the signal's direction (call for long, put for
                 short) at the NEXT minute's open; sell at the 15:15 close
  control        the opposite option (put for long, call for short), same strike, same
                 times. If breakouts carry no information, both lose about the same to
                 time decay; the breakout side must clearly beat the control to count.

Costs per trade: Rs 40 brokerage (2 orders), STT on the sale, ~Rs 10 other charges, and
slippage of max(2 ticks, 0.5%) on each side, as in the fill model.

Variants, declared before running: N in {15, 30} x days to expiry {0, 1-2, 3+} x 2 indices
= 12 cells. All are reported and logged.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from tradeloop.charges import default_charges
from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST
from tradeloop.core.types import Side
from tradeloop.lot_sizes import default_lot_sizes

DEV_END = date(2024, 12, 31)
CUTOFF, EXIT = time(12, 0), time(15, 15)
STEP = {"NIFTY": 50, "SENSEX": 100}
MIN = 60_000


def _ms(d: date, t: time) -> int:
    return int(datetime.combine(d, t, IST).timestamp() * 1000)


def _slip(price: float) -> float:
    return max(0.10, price * 0.005)


def trade_pnl(entry: float, exit_: float, lot: int, d: date, iid: InstrumentId) -> tuple[float, float]:
    """(gross, cost) in rupees for buying one lot at entry and selling at exit_, with slippage."""
    buy, sell = entry + _slip(entry), max(0.05, exit_ - _slip(exit_))
    c = default_charges()
    charges = sum(c.trade(d, iid, Side.BUY, round(buy * 100) * lot).values()) + \
        sum(c.trade(d, iid, Side.SELL, round(sell * 100) * lot).values())
    return (exit_ - entry) * lot, (buy - entry + exit_ - sell) * lot + charges / 100


def study_expiry(name: str, expiry: date, previous: date, history: Path, n_values=(15, 30)) -> list[dict]:
    c = pd.read_parquet(history / "candles_1m" / name / f"{expiry}.parquet")
    exchange, underlying = name.split("_")
    index_id = f"{exchange}|{underlying}|IDX"
    c["day"] = pd.to_datetime(c["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date
    opts = c[c["instrument"].str.endswith(("CE", "PE"))].set_index(["instrument", "ts_ms"]).sort_index()
    lot = None
    rows = []
    for d in sorted(x for x in c["day"].unique() if previous < x <= min(expiry, DEV_END)):
        idx = c[(c["instrument"] == index_id) & (c["day"] == d) & (c["ts_ms"] >= _ms(d, time(9, 15)))]
        idx = idx.sort_values("ts_ms").set_index("ts_ms")
        exit_ts = _ms(d, EXIT) - MIN                        # candle that closes at 15:15
        for n in n_values:
            rng = idx[idx.index < _ms(d, time(9, 15)) + n * MIN]
            if len(rng) < n * 0.8:
                continue
            hi, lo = rng["high"].max(), rng["low"].min()
            scan = idx[(idx.index >= _ms(d, time(9, 15)) + n * MIN) & (idx.index < _ms(d, CUTOFF))]
            hit = scan[(scan["close"] > hi) | (scan["close"] < lo)]
            if hit.empty:
                rows.append({"index": name, "day": d.isoformat(), "dte": (expiry - d).days, "n": n,
                             "signal": "none", "or_pct": (hi - lo) / lo * 100})
                continue
            ts, spot = int(hit.index[0]), float(hit["close"].iloc[0])
            direction = "long" if spot > hi else "short"
            strike = round(spot / STEP[underlying]) * STEP[underlying]
            legs = {}
            for role, kind in (("trade", "CE" if direction == "long" else "PE"),
                               ("control", "PE" if direction == "long" else "CE")):
                iid = InstrumentId.option(exchange, underlying, expiry, float(strike), kind)
                try:
                    leg = opts.loc[str(iid)]
                except KeyError:
                    break
                entry = leg[leg.index >= ts + MIN]
                out = leg[(leg.index <= exit_ts) & (leg.index > ts + MIN)]
                if entry.empty or out.empty or entry.index[0] > ts + 5 * MIN:
                    break
                lot = lot or default_lot_sizes().lot_size(iid)
                gross, cost = trade_pnl(float(entry["open"].iloc[0]), float(out["close"].iloc[-1]), lot, d, iid)
                legs[role] = (gross, cost, float(entry["open"].iloc[0]))
            if len(legs) < 2:
                continue
            move = (float(idx.loc[idx.index <= exit_ts, "close"].iloc[-1]) - spot) / spot * 100
            rows.append({"index": name, "day": d.isoformat(), "dte": (expiry - d).days, "n": n,
                         "signal": direction, "or_pct": (hi - lo) / lo * 100,
                         "signal_time": datetime.fromtimestamp(ts / 1000, IST).strftime("%H:%M"),
                         "move_pct": move if direction == "long" else -move, "premium": legs["trade"][2],
                         "trade_net": legs["trade"][0] - legs["trade"][1], "trade_cost": legs["trade"][1],
                         "control_net": legs["control"][0] - legs["control"][1]})
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
    t = df[df["signal"] != "none"].copy()
    t["dte_group"] = pd.cut(t["dte"], [-1, 0, 2, 99], labels=["0 (expiry)", "1-2", "3+"])
    out = [f"{'index':>10} {'range':>5} {'days to exp':>11} {'trades':>6} {'continued':>9} "
           f"{'trade win':>9} {'trade avg':>9} {'control avg':>11} {'difference':>10} {'95% range of diff':>18}"]
    for (name, n, g), h in t.groupby(["index", "n", "dte_group"], observed=True):
        diff = h["trade_net"] - h["control_net"]
        se = diff.std() / np.sqrt(len(diff))
        out.append(f"{name:>10} {n:>4}m {str(g):>11} {len(h):>6} {(h['move_pct'] > 0).mean():>8.0%} "
                   f"{(h['trade_net'] > 0).mean():>8.0%} {h['trade_net'].mean():>9,.0f} {h['control_net'].mean():>11,.0f} "
                   f"{diff.mean():>10,.0f} {diff.mean() - 1.96 * se:>8,.0f} .. {diff.mean() + 1.96 * se:>6,.0f}")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--out", default="data/research/orb_study.csv")
    a = p.parse_args()
    df = run(Path(a.history))
    df.to_csv(a.out, index=False)
    print(summarise(df))
    none = df[df["signal"] == "none"].groupby(["index", "n"]).size()
    print("\nDays with no breakout before 12:00:", ", ".join(f"{i} {n}m: {c}" for (i, n), c in none.items()))


if __name__ == "__main__":
    main()
