"""SQLite storage for recorded market data. Simple now; swap for a time-series DB later.

Instruments are neutral IDs (design rule 2). Tables are append-only; the primary
keys make re-inserting the same update a no-op.
"""
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from tradeloop.core.types import Contract, Tick

SCHEMA = """
CREATE TABLE IF NOT EXISTS ltp (          -- option last traded price, or index value
    instrument TEXT, ts_ms INTEGER, recv_ms INTEGER, ltp REAL,
    volume REAL, oi REAL,                 -- day volume so far, open interest; NULL for indices
    PRIMARY KEY (instrument, ts_ms)
);
CREATE TABLE IF NOT EXISTS depth (
    instrument TEXT, ts_ms INTEGER, recv_ms INTEGER,
    bid_px REAL, bid_qty REAL, ask_px REAL, ask_qty REAL,
    book_json TEXT,                       -- {"buy": [[price, qty], ...], "sell": [...]}, best first
    PRIMARY KEY (instrument, ts_ms)
);
CREATE TABLE IF NOT EXISTS contracts (    -- which contracts were recorded each day
    session_date TEXT, instrument TEXT, lot_size INTEGER,
    PRIMARY KEY (session_date, instrument)
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def write_ltp(self, rows):
        self._many("INSERT OR IGNORE INTO ltp VALUES (?,?,?,?,?,?)", rows)

    def write_depth(self, rows):
        self._many("INSERT OR IGNORE INTO depth VALUES (?,?,?,?,?,?,?,?)", rows)

    def save_contracts(self, session_date: str, contracts: Iterable[Contract]):
        rows = [(session_date, str(c.instrument), c.lot_size) for c in contracts]
        self._many("INSERT OR IGNORE INTO contracts VALUES (?,?,?)", rows)

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _many(self, sql, rows):
        rows = list(rows)
        if rows:
            with self.conn:
                self.conn.executemany(sql, rows)

    def close(self):
        self.conn.close()


class TickWriter:
    """Bus subscriber that stores Ticks, one transaction per batch rather than per tick."""

    def __init__(self, store: Store, max_rows: int = 1000, max_seconds: float = 1.0,
                 clock: Callable[[], float] = time.monotonic):
        self.store, self.max_rows, self.max_seconds, self.clock = store, max_rows, max_seconds, clock
        self._ltp: list[tuple] = []
        self._depth: list[tuple] = []
        self._last_flush = clock()

    def on_tick(self, t: Tick) -> None:
        iid = str(t.instrument)
        if t.ltp is not None:
            self._ltp.append((iid, t.ts_ms, t.recv_ms, t.ltp, t.volume, t.oi))
        if t.bids or t.asks:
            book = {"buy": [[lv.price, lv.qty] for lv in t.bids],
                    "sell": [[lv.price, lv.qty] for lv in t.asks]}
            self._depth.append((iid, t.ts_ms, t.recv_ms, t.bid, t.bid_qty, t.ask, t.ask_qty,
                                json.dumps(book, separators=(",", ":"))))
        if (len(self._ltp) + len(self._depth) >= self.max_rows
                or self.clock() - self._last_flush >= self.max_seconds):
            self.flush()

    def flush(self) -> None:
        self.store.write_ltp(self._ltp)
        self.store.write_depth(self._depth)
        self._ltp, self._depth = [], []
        self._last_flush = self.clock()
