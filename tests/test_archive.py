import sqlite3
from datetime import date, datetime

import pandas as pd
import pytest

from tradeloop import archive as arch
from tradeloop.archive import IST, VerifyError, archive, archive_day, days_in_db, read_day
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Contract, Level, Tick
from tradeloop.store import Store, TickWriter

D1, D2 = date(2026, 10, 26), date(2026, 10, 27)
CE = InstrumentId.option("NSE", "NIFTY", D2, 25500, "CE")
NIFTY = InstrumentId.index("NSE", "NIFTY")


def ms(d: date, hh: int, mm: int) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp() * 1000)


def record(store: Store, d: date, n: int = 3, start_min: int = 15):
    w = TickWriter(store)
    for i in range(n):
        ts = ms(d, 9, start_min + i)
        w.on_tick(Tick(NIFTY, ts, ltp=25500.0 + i, recv_ms=ts + 5))
        w.on_tick(Tick(CE, ts, ltp=100.0 + i, recv_ms=ts + 5, volume=1000.0 * i, oi=50000.0))
        w.on_tick(Tick(CE, ts + 1, recv_ms=ts + 6, bid=99.9, bid_qty=65, ask=100.1, ask_qty=130,
                       bids=(Level(99.9, 65),), asks=(Level(100.1, 130),)))
    w.flush()
    store.save_contracts(d.isoformat(), [Contract(CE, 65)])


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "ticks.db")
    record(store, D1)
    record(store, D2)
    store.close()
    return tmp_path / "ticks.db", tmp_path / "archive"


def snapshot(db, d):
    return {t: read_day(t, d, db, "/nonexistent") for t in arch.TABLES}


def test_moves_finished_days_and_keeps_today(db):
    path, out = db
    before = snapshot(path, D1)

    done = archive(path, out, today=D2)

    assert done == {D1: {"ltp": 6, "depth": 3, "contracts": 1}}
    assert sorted(f.name for f in (out / "2026-10-26").iterdir()) == \
        ["contracts.parquet", "depth.parquet", "ltp.parquet"]
    assert days_in_db(sqlite3.connect(path)) == [D2]          # only today left
    for table, df in before.items():                                         # identical data back
        pd.testing.assert_frame_equal(read_day(table, D1, path, out), df, check_dtype=False)
    assert read_day("ltp", D2, path, out)["ltp"].tolist() == [100.0, 101.0, 102.0, 25500.0, 25501.0, 25502.0]


def test_rerun_is_a_no_op(db):
    path, out = db
    archive(path, out, today=D2)
    assert archive(path, out, today=D2) == {}
    assert len(pd.read_parquet(out / "2026-10-26" / "ltp.parquet")) == 6


def test_half_finished_run_is_merged_not_replaced(db, tmp_path):
    path, out = db
    archive(path, out, today=D2)
    store = Store(path)
    record(store, D1, n=2, start_min=40)            # more rows for the archived day show up
    store.close()
    archive(path, out, today=D2)
    assert len(pd.read_parquet(out / "2026-10-26" / "ltp.parquet")) == 6 + 4
    assert len(pd.read_parquet(out / "2026-10-26" / "contracts.parquet")) == 1   # deduped


def test_failed_verification_deletes_nothing(db, monkeypatch):
    path, out = db

    class Lying:
        num_rows = -1

    monkeypatch.setattr(arch.pq, "read_metadata", lambda p: Lying())
    with pytest.raises(VerifyError):
        archive(path, out, today=D2)
    assert len(read_day("ltp", D1, path, "/nonexistent")) == 6                # still in SQLite


def test_days_follow_ist_not_utc(tmp_path):
    store = Store(tmp_path / "ticks.db")
    store.write_ltp([("NSE|NIFTY|IDX", ms(D2, 0, 30), 0, 1.0, None, None),   # 00:30 IST = 19:00 UTC on D1
                     ("NSE|NIFTY|IDX", ms(D1, 23, 59), 0, 2.0, None, None)])
    assert days_in_db(store.conn) == [D1, D2]
    store.conn.isolation_level = None
    archive_day(store.conn, D1, tmp_path / "archive")
    assert pd.read_parquet(tmp_path / "archive" / "2026-10-26" / "ltp.parquet")["ltp"].tolist() == [2.0]


# --- chain.db: same command, same day folder ---------------------------------------------

def chain_db(tmp_path, days=(D1, D2)):
    from tradeloop.chain_recorder import SCHEMA

    path = tmp_path / "chain.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for d in days:
        for i in range(3):
            ts = ms(d, 9, 15 + i)
            conn.execute("INSERT INTO chain VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (str(CE), ts, 25500.0, 100.0 + i, 1000.0, 50000.0, 12.5, 0.4, 0.001, -3.0, 5.0, 0.1))
    conn.commit()
    conn.close()
    return path


def test_chain_db_archives_next_to_the_tick_files(db, tmp_path):
    ticks, out = db
    chain = chain_db(tmp_path)
    before = read_day("chain", D1, chain, "/nonexistent")

    archive(ticks, out, today=D2)
    done = archive(chain, out, today=D2)

    assert done == {D1: {"chain": 3}}
    assert sorted(f.name for f in (out / "2026-10-26").iterdir()) == \
        ["chain.parquet", "contracts.parquet", "depth.parquet", "ltp.parquet"]
    assert days_in_db(sqlite3.connect(chain)) == [D2]                       # today stays in SQLite
    pd.testing.assert_frame_equal(read_day("chain", D1, chain, out), before, check_dtype=False)
    assert len(read_day("ltp", D1, ticks, out)) == 6                        # tick files untouched


def test_chain_archive_is_safe_to_run_twice_and_an_untouched_table_is_not_an_error(tmp_path):
    chain, out = chain_db(tmp_path), tmp_path / "archive"
    archive(chain, out, today=D2)
    assert archive(chain, out, today=D2) == {}
    assert read_day("ltp", D1, chain, out).empty                            # chain.db has no ltp table
