"""Daily health check: did we record the whole session?

    python -m tradeloop.health --db data/ticks.db [--date 2026-10-27]

Reports rows per table and any minute between 09:15 and 15:30 IST
with no index updates (a gap you can't fill later).
"""
import argparse
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def day_bounds_ms(d: date):
    start = datetime.combine(d, time(9, 15), IST)
    end = datetime.combine(d, time(15, 30), IST)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def gap_minutes(conn, d: date, exchange: str, token: str):
    lo, hi = day_bounds_ms(d)
    rows = conn.execute(
        "SELECT DISTINCT (ts_ms - ?) / 60000 FROM index_value "
        "WHERE exchange=? AND token=? AND ts_ms BETWEEN ? AND ?",
        (lo, exchange, token, lo, hi)).fetchall()
    seen = {r[0] for r in rows}
    total = (hi - lo) // 60000
    start = datetime.fromtimestamp(lo / 1000, IST)
    return [(start + timedelta(minutes=m)).strftime("%H:%M") for m in range(total) if m not in seen]


def report(db: str, d: date):
    conn = sqlite3.connect(db)
    lo, hi = day_bounds_ms(d)
    print(f"Health for {d.isoformat()} ({db})")
    for table in ("index_value", "ltp", "depth"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE ts_ms BETWEEN ? AND ?", (lo, hi)).fetchone()[0]
        print(f"  {table:12s} {n:>10,d} rows")
    n_contracts = conn.execute("SELECT COUNT(*) FROM session_contracts WHERE session_date=?",
                               (d.isoformat(),)).fetchone()[0]
    print(f"  contracts    {n_contracts:>10,d} subscribed")
    for exchange, token in conn.execute("SELECT DISTINCT exchange, token FROM index_value"):
        gaps = gap_minutes(conn, d, exchange, token)
        status = "no gaps" if not gaps else f"{len(gaps)} empty minutes, first {gaps[:5]}"
        print(f"  index {exchange}:{token:8s} {status}")
    conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/ticks.db")
    p.add_argument("--date", default=datetime.now(IST).date().isoformat())
    a = p.parse_args()
    report(a.db, date.fromisoformat(a.date))


if __name__ == "__main__":
    main()
