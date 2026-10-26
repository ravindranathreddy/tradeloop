"""SQLite storage for recorded market data. Simple now; swap for a time-series DB later.

Tables are append-only. The primary keys make re-inserting the same update a no-op.
"""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ltp (
    ts_ms INTEGER, recv_ms INTEGER, exchange TEXT, segment TEXT, token TEXT, ltp REAL,
    PRIMARY KEY (exchange, token, ts_ms)
);
CREATE TABLE IF NOT EXISTS index_value (
    ts_ms INTEGER, recv_ms INTEGER, exchange TEXT, token TEXT, value REAL,
    PRIMARY KEY (exchange, token, ts_ms)
);
CREATE TABLE IF NOT EXISTS depth (
    ts_ms INTEGER, recv_ms INTEGER, exchange TEXT, segment TEXT, token TEXT,
    bid_px REAL, bid_qty REAL, ask_px REAL, ask_qty REAL, book_json TEXT,
    PRIMARY KEY (exchange, token, ts_ms)
);
CREATE TABLE IF NOT EXISTS session_contracts (
    session_date TEXT, exchange TEXT, token TEXT, trading_symbol TEXT, underlying TEXT,
    expiry TEXT, strike REAL, option_type TEXT, lot_size INTEGER,
    PRIMARY KEY (session_date, exchange, token)
);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def write_ltp(self, rows):
        self._many("INSERT OR IGNORE INTO ltp VALUES (?,?,?,?,?,?)", rows)

    def write_index(self, rows):
        self._many("INSERT OR IGNORE INTO index_value VALUES (?,?,?,?,?)", rows)

    def write_depth(self, rows):
        self._many("INSERT OR IGNORE INTO depth VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def save_session(self, session_date: str, contracts):
        rows = [
            (session_date, r["exchange"], str(r["exchange_token"]), r["trading_symbol"],
             r["underlying_symbol"], str(r["expiry_date"]), float(r["strike_price"]),
             r["instrument_type"], int(r["lot_size"]))
            for _, r in contracts.iterrows()
        ]
        self._many("INSERT OR IGNORE INTO session_contracts VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _many(self, sql, rows):
        rows = list(rows)
        if rows:
            with self.conn:
                self.conn.executemany(sql, rows)

    def close(self):
        self.conn.close()
