"""Option-chain recorder: once a minute, Groww's full option chain for each index's
nearest expiry, into SQLite (data/chain.db, table `chain`, neutral IDs).

    python -m tradeloop.chain_recorder [--db data/chain.db] [--seconds 60]

Fills what the live feed lacks: the feed carries prices and the order book but almost
never volume or OI, and no IV or Greeks. One call per index returns every strike's LTP,
volume, OI, IV and Greeks (Groww's, in 0.3 s).

Groww's Greeks are a cross-check and a record, not what strategies use: strategies use
our own (tradeloop.greeks), so live and backtest see the same definitions. Known
difference: Groww's theta looks capped at the option's price; ours is the instantaneous
rate per calendar day, which on expiry day exceeds the premium. Groww's iv is a percent;
oi is in contracts (lots), not units: Sensibull's OI charts show units (oi x lot size).

The chain has no timestamp, so ts_ms is when we received it. Stops at the F&O close.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import time as _time
from datetime import date, datetime
from pathlib import Path

from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST, default_calendar

log = logging.getLogger("chain")
INDICES = [InstrumentId.index("NSE", "NIFTY"), InstrumentId.index("BSE", "SENSEX")]
FIELDS = ["ltp", "volume", "oi", "iv", "delta", "gamma", "theta", "vega", "rho"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain (
    instrument TEXT NOT NULL, ts_ms INTEGER NOT NULL, underlying_ltp REAL,
    ltp REAL, volume REAL, oi REAL, iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,
    PRIMARY KEY (instrument, ts_ms)
);
"""


def chain_rows(index: InstrumentId, expiry: date, chain: dict, ts_ms: int) -> list[tuple]:
    """Groww option chain -> rows (instrument, ts_ms, underlying_ltp, ltp, volume, oi, iv, ...)."""
    spot = chain.get("underlying_ltp")
    rows = []
    for strike, sides in (chain.get("strikes") or {}).items():
        for kind, q in (sides or {}).items():
            if kind not in ("CE", "PE") or not q:
                continue
            g = q.get("greeks") or {}
            iid = InstrumentId.option(index.exchange, index.underlying, expiry, float(strike), kind)
            rows.append((str(iid), ts_ms, spot, q.get("ltp"), q.get("volume"), q.get("open_interest"),
                         g.get("iv"), g.get("delta"), g.get("gamma"), g.get("theta"), g.get("vega"), g.get("rho")))
    return rows


def poll_once(api, db: sqlite3.Connection, today: date) -> int:
    cal = default_calendar()
    n = 0
    for index in INDICES:
        expiry = cal.next_expiry(index, today)
        try:
            chain = api.get_option_chain(exchange=index.exchange, underlying=index.underlying,
                                         expiry_date=expiry.isoformat())
        except Exception as e:                       # one bad poll mustn't stop the day
            log.warning("%s chain failed: %s", index, str(e)[:120])
            continue
        rows = chain_rows(index, expiry, chain, int(_time.time() * 1000))
        db.executemany(f"INSERT OR IGNORE INTO chain VALUES ({','.join('?' * 12)})", rows)
        n += len(rows)
    db.commit()
    return n


def main():
    from tradeloop.auth import login

    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/chain.db")
    p.add_argument("--seconds", type=float, default=60)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = datetime.now(IST).date()
    if not default_calendar().is_trading_day(today):
        log.info("Not a trading day; nothing to record")
        return
    close = datetime.combine(today, default_calendar().close_time(today), IST)
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(a.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    api = login()
    while datetime.now(IST) <= close:
        started = _time.monotonic()
        n = poll_once(api, db, today)
        log.info("chain: %d rows", n)
        _time.sleep(max(0.0, a.seconds - (_time.monotonic() - started)))
    db.close()


if __name__ == "__main__":
    main()
