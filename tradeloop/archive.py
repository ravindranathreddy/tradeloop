"""Move finished days from SQLite to compressed Parquet. Every day is kept forever.

    python -m tradeloop.archive --db data/ticks.db --out data/archive

Run after the recorder each day. For every day before today (IST) still in
SQLite, it writes data/archive/<date>/<table>.parquet (zstd), reads the files
back to check row counts, and only then deletes that day from SQLite. SQLite
reuses the freed pages, so the database stays about one day in size.

If a day's files already exist (an earlier run stopped half way), the rows are
merged, never replaced, so a re-run can't lose data.
"""
import argparse
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.parquet as pq

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("archive")

# table -> (primary key, how a row belongs to a day)
TABLES = {
    "ltp": (["instrument", "ts_ms"], "ts"),
    "depth": (["instrument", "ts_ms"], "ts"),
    "contracts": (["session_date", "instrument"], "session"),
}


class VerifyError(RuntimeError):
    pass


def day_bounds_ms(d: date) -> tuple[int, int]:
    """[start, end) of an IST calendar day in epoch ms."""
    start = datetime.combine(d, time(0), IST)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def _where(kind: str, d: date) -> tuple[str, tuple]:
    if kind == "session":
        return "session_date = ?", (d.isoformat(),)
    return "ts_ms >= ? AND ts_ms < ?", day_bounds_ms(d)


def days_in_db(conn: sqlite3.Connection) -> list[date]:
    ist_offset_ms = 330 * 60 * 1000
    days = {date.fromisoformat(r[0]) for r in conn.execute("SELECT DISTINCT session_date FROM contracts")}
    for table in ("ltp", "depth"):
        for (n,) in conn.execute(f"SELECT DISTINCT (ts_ms + ?) / 86400000 FROM {table}", (ist_offset_ms,)):
            days.add(date(1970, 1, 1) + timedelta(days=n))
    return sorted(days)


def _write_merged(df: pd.DataFrame, path: Path, key: list[str]) -> int:
    """Write df to path, merged with whatever is already there. Returns rows in the file."""
    if path.exists():
        df = pd.concat([pd.read_parquet(path), df]).drop_duplicates(subset=key)
    df = df.sort_values(key).reset_index(drop=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, compression="zstd", index=False)
    os.replace(tmp, path)            # atomic: a crash leaves the old file or the new one, never half
    return len(df)


def archive_day(conn: sqlite3.Connection, d: date, out_dir: Path) -> dict[str, int]:
    """Archive one day and delete it from SQLite. Returns rows archived per table."""
    day_dir = out_dir / d.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    moved = {}
    conn.execute("BEGIN IMMEDIATE")  # no writer can add rows for this day mid-way
    try:
        for table, (key, kind) in TABLES.items():
            where, args = _where(kind, d)
            df = pd.read_sql_query(f"SELECT * FROM {table} WHERE {where}", conn, params=args)
            if df.empty:
                continue
            path = day_dir / f"{table}.parquet"
            expected = _write_merged(df, path, key)
            stored = pq.read_metadata(path).num_rows
            if stored != expected:
                raise VerifyError(f"{path}: wrote {expected} rows, file has {stored}")
            back = pd.read_parquet(path, columns=key)
            missing = len(df.merge(back, on=key, how="left", indicator=True).query("_merge == 'left_only'"))
            if missing:
                raise VerifyError(f"{path}: {missing} SQLite rows not in the file")
            conn.execute(f"DELETE FROM {table} WHERE {where}", args)
            moved[table] = len(df)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return moved


def archive(db: str | Path, out_dir: str | Path, today: date) -> dict[date, dict[str, int]]:
    """Archive every day before `today` that's still in SQLite."""
    conn = sqlite3.connect(str(db), isolation_level=None)   # we manage transactions ourselves
    try:
        done = {}
        for d in days_in_db(conn):
            if d < today:
                done[d] = archive_day(conn, d, Path(out_dir))
        return done
    finally:
        conn.close()


def read_day(table: str, d: date, db: str | Path, out_dir: str | Path) -> pd.DataFrame:
    """One day of a table, wherever it lives now (archive, SQLite, or both)."""
    key, kind = TABLES[table]
    parts = []
    path = Path(out_dir) / d.isoformat() / f"{table}.parquet"
    if path.exists():
        parts.append(pd.read_parquet(path))
    if Path(db).exists():
        conn = sqlite3.connect(str(db))
        where, args = _where(kind, d)
        parts.append(pd.read_sql_query(f"SELECT * FROM {table} WHERE {where}", conn, params=args))
        conn.close()
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts).drop_duplicates(subset=key).sort_values(key).reset_index(drop=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/ticks.db")
    p.add_argument("--out", default="data/archive")
    p.add_argument("--today", default=datetime.now(IST).date().isoformat(),
                   help="archive days before this one (default: today, IST)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        done = archive(a.db, a.out, date.fromisoformat(a.today))
    except VerifyError as e:
        log.error("Verification failed, nothing deleted for that day: %s", e)
        sys.exit(1)
    if not done:
        log.info("Nothing to archive")
    for d, moved in done.items():
        size = sum(f.stat().st_size for f in (Path(a.out) / d.isoformat()).glob("*.parquet"))
        log.info("%s: %s -> %.1f MB", d, ", ".join(f"{t}={n:,}" for t, n in moved.items()), size / 1e6)


if __name__ == "__main__":
    main()
