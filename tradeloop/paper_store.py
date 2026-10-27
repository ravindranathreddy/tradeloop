"""Paper trading records in SQLite (data/paper.db), so positions survive restarts: a spread
opened on Monday is still there when Tuesday's process starts.

The ledger itself isn't stored: it's rebuilt by replaying fills and expiry settlements
in time order, which gives the same books every time (double entry is deterministic).

    fills         every paper fill
    settlements   expiry settlements (official close used)
    orders        every order and its last status, including risk rejections
    strategy      each strategy's state for the current expiry week (JSON)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date
from pathlib import Path

from tradeloop.charges import ChargeSchedule
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Order, OrderUpdate, Side
from tradeloop.ledger import Ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, client_ref TEXT NOT NULL,
    instrument TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL, ts_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    strategy_id TEXT NOT NULL, expiry TEXT NOT NULL, underlying TEXT NOT NULL, price REAL NOT NULL,
    ts_ms INTEGER NOT NULL, PRIMARY KEY (strategy_id, expiry, underlying)
);
CREATE TABLE IF NOT EXISTS orders (
    client_ref TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, config_version TEXT NOT NULL,
    instrument TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, order_type TEXT NOT NULL,
    placed_ms INTEGER NOT NULL, status TEXT NOT NULL, message TEXT
);
CREATE TABLE IF NOT EXISTS strategy (
    strategy_id TEXT NOT NULL, week TEXT NOT NULL, state TEXT NOT NULL, updated_ms INTEGER NOT NULL,
    PRIMARY KEY (strategy_id, week)
);
"""


class PaperStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)   # feed thread writes; lock below
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def _write(self, sql: str, args: tuple) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    def save_fill(self, strategy_id: str, f: Fill) -> None:
        self._write("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?)",
                    (f.fill_id, strategy_id, f.client_ref, str(f.instrument), f.side.value, f.qty, f.price, f.ts_ms))

    def save_order(self, o: Order, placed_ms: int) -> None:
        self._write("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (o.client_ref, o.strategy_id, o.config_version, str(o.instrument), o.side.value, o.qty,
                     o.order_type.value, placed_ms, "NEW", None))

    def update_order(self, u: OrderUpdate) -> None:
        self._write("UPDATE orders SET status = ?, message = COALESCE(?, message) WHERE client_ref = ?",
                    (u.status.value, u.message, u.client_ref))

    def save_settlement(self, strategy_id: str, expiry: date, underlying: str, price: float, ts_ms: int) -> None:
        self._write("INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?)",
                    (strategy_id, expiry.isoformat(), underlying, price, ts_ms))

    def save_state(self, strategy_id: str, week: date, state: dict, ts_ms: int) -> None:
        self._write("INSERT OR REPLACE INTO strategy VALUES (?,?,?,?)",
                    (strategy_id, week.isoformat(), json.dumps(state), ts_ms))

    def load_state(self, strategy_id: str, week: date) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT state FROM strategy WHERE strategy_id = ? AND week = ?",
                                   (strategy_id, week.isoformat())).fetchone()
        return json.loads(row[0]) if row else None

    def query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def rebuild_ledger(self, strategy_id: str, charges: ChargeSchedule | None = None) -> Ledger:
        """Replay fills and settlements in time order into a fresh ledger."""
        ledger = Ledger(charges)
        events = [(ts, 0, ("fill", fid, ref, inst, side, qty, price)) for fid, ref, inst, side, qty, price, ts in self.query(
            "SELECT fill_id, client_ref, instrument, side, qty, price, ts_ms FROM fills WHERE strategy_id = ?", (strategy_id,))]
        events += [(ts, 1, ("settle", exp, und, price)) for exp, und, price, ts in self.query(
            "SELECT expiry, underlying, price, ts_ms FROM settlements WHERE strategy_id = ?", (strategy_id,))]
        for ts, _, e in sorted(events, key=lambda x: (x[0], x[1])):
            if e[0] == "fill":
                _, fid, ref, inst, side, qty, price = e
                ledger.record_fill(Fill(fid, ref, InstrumentId.parse(inst), Side(side), qty, price, ts))
            else:
                _, exp, und, price = e
                ledger.settle_expiry(date.fromisoformat(exp), und, price, ts)
        return ledger

    def close(self) -> None:
        self._db.close()
