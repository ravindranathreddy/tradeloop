"""Download 1-minute candles for past weekly expiries (index + options) into Parquet.

    python -m tradeloop.backfill --from 2025-06-01 --to 2025-06-30
    python -m tradeloop.backfill                      # everything Groww has, both indices

For each expiry the broker lists: the index's candles for the expiry's final week
(expiry - 6 days to expiry), then every option strike from the week's low to its
high, plus `extra_strikes` either side, CE and PE, and the future if one expires
that day. One call per contract covers the whole week.

Strikes come from the strike grid (STRIKE_STEP), not Groww's contract list: the list
leaves out contracts that exist (e.g. NIFTY 15 Sep 2026 23500 CE, unlisted, 1,543
candles), up to a third of strikes in 2026. A strike that doesn't exist returns no
candles, which costs one call and nothing else. Output, per expiry:

    data/history/candles_1m/<exchange>_<index>/<expiry>.parquet   index, options, future
    data/history/candles_1d/<exchange>_<index>/<expiry>.parquet   index daily candles
        instrument, ts_ms, open, high, low, close, volume, oi

Index 1-minute candles include the pre-open auction (09:00-09:14) and post-close
prints to 16:00; options start at 09:15 and trade until the F&O close (15:30, 15:40
from 3 Aug 2026; see market_calendar.yaml). Expiring options settle on the official close
(a weighted average of the last 30 minutes), which is the daily candle's close, not
the last 1-minute close.

Raw broker data only; IV and Greeks are derived later from these files.

Ghost expiries: brokers list some expiries that never traded (e.g. SENSEX Friday
weeklies left over after BSE moved to Tuesday). If the ATM call and put have no
volume, the expiry is marked ghost and skipped. Every expiry's outcome goes in
data/history/expiries.csv; that file is the historical expiry list for backtests.

Resumable: expiries already marked ok, ghost or no_index_data are skipped; failed
ones are retried on the next run. --refetch downloads a range again regardless.
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.market_calendar import default_calendar

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("backfill")

COLUMNS = ["instrument", "ts_ms", "open", "high", "low", "close", "volume", "oi"]
MANIFEST_FIELDS = ["index", "expiry", "status", "contracts", "rows", "note", "fetched_at"]
DONE = {"ok", "ghost", "no_index_data"}
INDICES = [InstrumentId.index("NSE", "NIFTY"), InstrumentId.index("BSE", "SENSEX")]
FIRST_YEAR = 2020
STRIKE_STEP = {"NIFTY": 50, "SENSEX": 100}      # weekly option strike grid near the money


def week_window(expiry: date) -> tuple[datetime, datetime]:
    return (datetime.combine(expiry - timedelta(days=6), time(9, 0), IST),
            datetime.combine(expiry, default_calendar().close_time(expiry), IST))


def strikes_to_fetch(index: InstrumentId, expiry: date, low: float, high: float, extra: int,
                     step: float) -> list[InstrumentId]:
    """CE and PE on the strike grid from the week's low to high, plus `extra` strikes either side."""
    first = math.ceil((low - extra * step) / step)
    last = math.floor((high + extra * step) / step)
    return [InstrumentId.option(index.exchange, index.underlying, expiry, float(k * step), kind)
            for k in range(first, last + 1) for kind in ("CE", "PE")]


def _traded(rows: list[tuple]) -> bool:
    return any((r[6] or 0) > 0 for r in rows)


def backfill_expiry(hist, index: InstrumentId, expiry: date, out_dir: Path, extra: int = 10) -> dict:
    """Fetch one expiry's week and write it. Returns a manifest row."""
    row = {"index": str(index), "expiry": expiry.isoformat(), "contracts": 0, "rows": 0, "note": ""}
    start, end = week_window(expiry)
    index_rows = hist.candles(index, start, end)
    expiry_ms = int(datetime.combine(expiry, time(0), IST).timestamp() * 1000)
    on_expiry_day = [r for r in index_rows if r[1] >= expiry_ms]
    if not on_expiry_day:
        return {**row, "status": "no_index_data", "note": "no index candles on expiry day"}

    listed = [c for c in hist.contracts(index, expiry) if c.expiry == expiry]
    options = [c for c in listed if c.kind.is_option]
    futures = [c for c in listed if c.kind is Kind.FUT]
    if not options:
        return {**row, "status": "ghost", "note": "no contracts listed"}

    after_open = [r for r in on_expiry_day if r[1] >= expiry_ms + (9 * 60 + 15) * 60_000] or on_expiry_day
    open_price = after_open[0][2]                       # 09:15, not the pre-open auction
    step = STRIKE_STEP[index.underlying]
    atm_strike = round(open_price / step) * step
    fetched: dict[InstrumentId, list[tuple]] = {}
    for kind in ("CE", "PE"):                           # cheap ghost check before the full fetch
        atm = InstrumentId.option(index.exchange, index.underlying, expiry, float(atm_strike), kind)
        fetched[atm] = hist.candles(atm, start, end)
    if not any(_traded(rows) for rows in fetched.values()):
        return {**row, "status": "ghost", "note": "ATM call and put never traded"}

    after_open_week = [r for r in index_rows if (r[1] - expiry_ms) % 86_400_000 >= (9 * 60 + 15) * 60_000]
    low = min(r[4] for r in after_open_week or index_rows)   # pre-open prints can be far off
    high = max(r[3] for r in after_open_week or index_rows)
    grid = strikes_to_fetch(index, expiry, low, high, extra, step)
    listed = set(options)
    wanted = grid + futures
    for o in wanted:
        if o not in fetched:
            fetched[o] = hist.candles(o, start, end)

    daily = hist.candles(index, start, end, interval="1day")
    name = f"{index.exchange}_{index.underlying}"
    _write(daily, out_dir / "candles_1d" / name / f"{expiry.isoformat()}.parquet")
    df = _write(index_rows + [r for o in wanted for r in fetched.get(o, [])],
                out_dir / "candles_1m" / name / f"{expiry.isoformat()}.parquet")
    with_data = [o for o in wanted if fetched.get(o)]
    unlisted = sum(1 for o in with_data if o.kind.is_option and o not in listed)
    no_data = len(wanted) - len(with_data)
    notes = [f"{no_data} grid contracts have no candles"] if no_data else []
    if unlisted:
        notes.append(f"{unlisted} contracts missing from Groww's list had data")
    return {**row, "status": "ok", "contracts": len(with_data), "rows": len(df), "note": "; ".join(notes)}


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Groww returns some minutes more than once: exact copies, or a partial candle beside
    the full one (less volume, no OI). Keep the fullest. Rows priced 0 (the 18 May 2024
    weekend test session) or with no prices (futures' pre-open minutes) are dropped."""
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    ranked = df.assign(_v=df["volume"].fillna(-1), _oi=df["oi"].notna())
    ranked = ranked.sort_values(["instrument", "ts_ms", "_v", "_oi"], kind="stable")
    return (ranked.drop_duplicates(["instrument", "ts_ms"], keep="last")
            .drop(columns=["_v", "_oi"]).reset_index(drop=True))


def _write(rows: list[tuple], path: Path) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = clean(pd.DataFrame(rows, columns=COLUMNS).astype({"volume": "float64", "oi": "float64"}))
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, compression="zstd", index=False)
    os.replace(tmp, path)            # atomic: never a half-written file
    return df


def read_manifest(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {(r["index"], r["expiry"]): r for r in csv.DictReader(f)}


def write_manifest(path: Path, rows: dict[tuple[str, str], dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for key in sorted(rows):
            w.writerow({k: rows[key].get(k, "") for k in MANIFEST_FIELDS})
    os.replace(tmp, path)


def backfill(hist, indices: list[InstrumentId], start: date, end: date, out_dir: Path,
             extra: int = 10, today: date | None = None, refetch: bool = False) -> dict[tuple[str, str], dict]:
    """Backfill every listed expiry in [start, end] that's already over. `refetch` downloads
    finished ones again (e.g. after a session-time fix)."""
    today = today or datetime.now(IST).date()
    manifest_path = out_dir / "expiries.csv"
    manifest = read_manifest(manifest_path)
    for index in indices:
        for year in range(start.year, end.year + 1):
            for expiry in hist.expiries(index, year):
                key = (str(index), expiry.isoformat())
                if not (start <= expiry <= end) or expiry >= today:
                    continue
                if not refetch and manifest.get(key, {}).get("status") in DONE:
                    continue
                try:
                    row = backfill_expiry(hist, index, expiry, out_dir, extra)
                except Exception as e:
                    log.exception("%s %s failed", index, expiry)
                    row = {"index": str(index), "expiry": expiry.isoformat(), "status": "failed",
                           "contracts": 0, "rows": 0, "note": str(e)[:200]}
                row["fetched_at"] = datetime.now(IST).isoformat(timespec="seconds")
                manifest[key] = row
                write_manifest(manifest_path, manifest)       # after every expiry: safe to stop any time
                log.info("%s %s: %s %s contracts, %s rows %s", index, expiry, row["status"],
                         row["contracts"], row["rows"], row["note"])
    return manifest


def main():
    from tradeloop.auth import login
    from tradeloop.brokers.groww import GrowwHistory

    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default=f"{FIRST_YEAR}-01-01")
    p.add_argument("--to", dest="end", default=datetime.now(IST).date().isoformat())
    p.add_argument("--index", action="append", help="e.g. NSE|NIFTY|IDX (default: NIFTY and SENSEX)")
    p.add_argument("--extra-strikes", type=int, default=10)
    p.add_argument("--out", default="data/history")
    p.add_argument("--refetch", action="store_true", help="download finished expiries in the range again")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    indices = [InstrumentId.parse(i) for i in a.index] if a.index else INDICES
    hist = GrowwHistory(login())
    manifest = backfill(hist, indices, date.fromisoformat(a.start), date.fromisoformat(a.end),
                        Path(a.out), a.extra_strikes, refetch=a.refetch)
    counts: dict[str, int] = {}
    for r in manifest.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log.info("Done: %s, %d API calls", counts, hist.calls)


if __name__ == "__main__":
    main()
