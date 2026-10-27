"""Derive IV and Greeks from downloaded 1-minute candles. Re-runnable any time.

    python -m tradeloop.derive_greeks --history data/history

For every data/history/candles_1m/<index>/<expiry>.parquet without an up-to-date
data/history/greeks_1m/<index>/<expiry>.parquet, it writes one row per option
per minute that traded:

    instrument, ts_ms, close, forward, t_years, iv, delta, gamma, vega, theta, flag

forward: synthetic forward from put-call parity at the strike nearest the index
where both call and put traded that minute; if none did, the last one within 5
minutes (flag fwd_carried); otherwise no IV (flag no_forward).

flag (empty when clean): near_expiry (under a minute left), min_price (at the
0.05 tick, IV meaningless), no_iv (no volatility fits the price, e.g. below
intrinsic), plus fwd_carried / no_forward above. Several flags join with "|".
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tradeloop.core.market_calendar import default_calendar
from tradeloop.greeks import RATE, greeks, implied_vol

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("greeks")
MS_PER_YEAR = 365 * 24 * 3600 * 1000
FORWARD_TOLERANCE_MS = 5 * 60 * 1000
MIN_PRICE = 0.05
OUT_COLUMNS = ["instrument", "ts_ms", "close", "forward", "t_years", "iv",
               "delta", "gamma", "vega", "theta", "flag"]


def expiry_ms(expiry: str) -> int:
    d = datetime.fromisoformat(expiry).date()
    return int(datetime.combine(d, default_calendar().close_time(d), IST).timestamp() * 1000)


def split_instruments(df: pd.DataFrame) -> pd.DataFrame:
    """Add kind / expiry / strike columns parsed from neutral IDs."""
    parts = df["instrument"].str.split("|")
    out = df.assign(kind=parts.str[-1])
    opts = out["kind"].isin(["CE", "PE"])
    out["expiry"] = np.where(opts, parts.str[2], None)
    out["strike"] = np.where(opts, pd.to_numeric(parts.str[3], errors="coerce"), np.nan)
    return out


def synthetic_forward(options: pd.DataFrame, index: pd.DataFrame, exp_ms: int, r: float = RATE) -> pd.DataFrame:
    """Per minute: forward from the call/put pair nearest the index. Columns ts_ms, forward."""
    calls = options[options["kind"] == "CE"][["ts_ms", "strike", "close"]]
    puts = options[options["kind"] == "PE"][["ts_ms", "strike", "close"]]
    pairs = calls.merge(puts, on=["ts_ms", "strike"], suffixes=("_c", "_p"))
    if pairs.empty:
        return pd.DataFrame(columns=["ts_ms", "forward"])
    spot = pd.merge_asof(pairs.sort_values("ts_ms"), index[["ts_ms", "close"]].sort_values("ts_ms"),
                         on="ts_ms", direction="backward")
    spot = spot.dropna(subset=["close"])
    t = (exp_ms - spot["ts_ms"]) / MS_PER_YEAR
    spot["forward"] = spot["strike"] + (spot["close_c"] - spot["close_p"]) * np.exp(r * t)
    spot["dist"] = (spot["strike"] - spot["close"]).abs()
    best = spot.loc[spot.groupby("ts_ms")["dist"].idxmin(), ["ts_ms", "forward"]]
    return best.sort_values("ts_ms").reset_index(drop=True)


def derive(candles: pd.DataFrame, expiry: str, r: float = RATE) -> pd.DataFrame:
    df = split_instruments(candles)
    exp_ms = expiry_ms(expiry)
    index = df[df["kind"] == "IDX"]
    options = df[df["kind"].isin(["CE", "PE"]) & (df["expiry"] == expiry)].sort_values("ts_ms")
    if options.empty:
        return pd.DataFrame(columns=OUT_COLUMNS)

    fwd = synthetic_forward(options, index, exp_ms, r).rename(columns={"ts_ms": "fwd_ts"})
    out = options[["instrument", "ts_ms", "close", "kind", "strike"]].copy()
    if fwd.empty:
        out["forward"], out["fwd_ts"] = np.nan, np.nan
    else:
        out = pd.merge_asof(out, fwd.assign(ts_ms=fwd["fwd_ts"]), on="ts_ms",
                            direction="backward", tolerance=FORWARD_TOLERANCE_MS)

    out["t_years"] = (exp_ms - out["ts_ms"]) / MS_PER_YEAR
    is_call = (out["kind"] == "CE").to_numpy()
    F, K, T, price = (out[c].to_numpy(dtype=float) for c in ("forward", "strike", "t_years", "close"))
    out["iv"] = implied_vol(price, F, K, T, is_call, r)
    g = greeks(F, K, T, out["iv"].to_numpy(), is_call, r)
    for name, values in g.items():
        out[name] = np.where(np.isnan(out["iv"]), np.nan, values)

    flags = {
        "near_expiry": T < 60_000 / MS_PER_YEAR,
        "min_price": price <= MIN_PRICE,
        "no_forward": np.isnan(F),
        "fwd_carried": ~np.isnan(F) & (out["fwd_ts"].to_numpy() != out["ts_ms"].to_numpy()),
        "no_iv": out["iv"].isna().to_numpy() & ~np.isnan(F) & (T > 0),
    }
    text = np.full(len(out), "", dtype=object)
    for name, cond in flags.items():
        text = np.where(cond, text + name + "|", text)
    out["flag"] = pd.Series(text, index=out.index).str.rstrip("|")
    return out[OUT_COLUMNS].reset_index(drop=True)


def run(history: Path, force: bool = False) -> dict[str, int]:
    done = skipped = 0
    for src in sorted((history / "candles_1m").glob("*/*.parquet")):
        dst = history / "greeks_1m" / src.parent.name / src.name
        if not force and dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            skipped += 1
            continue
        result = derive(pd.read_parquet(src), src.stem)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        result.to_parquet(tmp, compression="zstd", index=False)
        os.replace(tmp, dst)
        clean = (result["flag"] == "").mean() if len(result) else 0
        log.info("%s/%s: %d rows, %.0f%% clean", src.parent.name, src.stem, len(result), clean * 100)
        done += 1
    return {"derived": done, "up_to_date": skipped}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--force", action="store_true", help="recompute even if up to date")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Done: %s", run(Path(a.history), a.force))


if __name__ == "__main__":
    main()
