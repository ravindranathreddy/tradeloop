"""Daily health check: did we record the whole session?

    python -m tradeloop.health --db data/ticks.db [--date 2026-10-27]

Reports rows per table, any minute between 09:15 and 15:30 IST with no
index updates (a gap you can't fill later), and disk headroom.
"""
import argparse
import shutil
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

GB = 1024 ** 3
IST = ZoneInfo("Asia/Kolkata")


def day_bounds_ms(d: date):
    start = datetime.combine(d, time(9, 15), IST)
    end = datetime.combine(d, time(15, 30), IST)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def gap_minutes(conn, d: date, instrument: str):
    lo, hi = day_bounds_ms(d)
    rows = conn.execute(
        "SELECT DISTINCT (ts_ms - ?) / 60000 FROM ltp WHERE instrument=? AND ts_ms BETWEEN ? AND ?",
        (lo, instrument, lo, hi)).fetchall()
    seen = {r[0] for r in rows}
    total = (hi - lo) // 60000
    start = datetime.fromtimestamp(lo / 1000, IST)
    return [(start + timedelta(minutes=m)).strftime("%H:%M") for m in range(total) if m not in seen]


def report(db: str, d: date):
    conn = sqlite3.connect(db)
    lo, hi = day_bounds_ms(d)
    print(f"Health for {d.isoformat()} ({db})")
    for table in ("ltp", "depth"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE ts_ms BETWEEN ? AND ?", (lo, hi)).fetchone()[0]
        print(f"  {table:12s} {n:>10,d} rows")
    n_contracts = conn.execute("SELECT COUNT(*) FROM contracts WHERE session_date=?",
                               (d.isoformat(),)).fetchone()[0]
    print(f"  contracts    {n_contracts:>10,d} subscribed")
    for (instrument,) in conn.execute("SELECT DISTINCT instrument FROM ltp WHERE instrument LIKE '%|IDX'"):
        gaps = gap_minutes(conn, d, instrument)
        status = "no gaps" if not gaps else f"{len(gaps)} empty minutes, first {gaps[:5]}"
        print(f"  {instrument:14s} {status}")
    days = conn.execute("SELECT COUNT(DISTINCT session_date) FROM contracts").fetchone()[0]
    conn.close()
    print("  " + disk_line(Path(db), days))


def disk_line(db: Path, days: int) -> str:
    size = sum(f.stat().st_size for f in db.parent.glob(db.name + "*"))   # .db plus -wal/-shm
    free = shutil.disk_usage(db.parent).free
    line = f"disk         {size / GB:.2f} GB used by {days} day(s), {free / GB:.1f} GB free"
    if days and size:
        line += f", ~{int(free / (size / days))} more days at this rate"
    return line


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/ticks.db")
    p.add_argument("--date", default=datetime.now(IST).date().isoformat())
    a = p.parse_args()
    report(a.db, date.fromisoformat(a.date))


if __name__ == "__main__":
    main()
