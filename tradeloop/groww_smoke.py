"""Read-only check of the real Groww account, to settle what the docs leave open.

    python -m tradeloop.groww_smoke [--spot 22936.75] [--lots 1]

Calls only: positions, available margin, today's orders, and the margin Groww would require for a
hypothetical put credit spread (a quote, not an order). It talks to Groww through ReadOnlyApi,
which refuses every method outside READ_ONLY, so no order can be placed or cancelled from here
even by mistake. It prints what Groww really returns (field names, how positions combine, the
list keys, the spread's margin) so the adapter's assumptions can be checked against facts.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from tradeloop.brokers import groww_orders as g
from tradeloop.brokers.groww_execution import GrowwExecution
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Order, OrderType, Product, Side

READ_ONLY = frozenset({"get_positions_for_user", "get_available_margin_details", "get_order_list",
                       "get_order_margin_details"})


class ReadOnlyApi:
    """Wraps a GrowwAPI and lets through only the methods in READ_ONLY."""

    def __init__(self, api):
        self._api = api

    def __getattr__(self, name):
        if name not in READ_ONLY:
            raise PermissionError(f"{name} is not allowed: this connection is read-only")
        return getattr(self._api, name)


def last_nifty_spot(db: str = "data/ticks.db") -> float | None:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = conn.execute("SELECT ltp FROM ltp WHERE instrument = 'NSE|NIFTY|IDX' ORDER BY ts_ms DESC LIMIT 1").fetchone()
        return float(row[0]) if row else None
    except sqlite3.Error:
        return None


def last_prices(ids, db: str = "data/ticks.db") -> dict:
    """Last recorded price of each contract (reference prices for the margin quote)."""
    out = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for i in ids:
            row = conn.execute("SELECT ltp FROM ltp WHERE instrument = ? ORDER BY ts_ms DESC LIMIT 1", (str(i),)).fetchone()
            if row:
                out[i] = float(row[0])
    except sqlite3.Error:
        pass
    return out


def spread_orders(symbols: dict[InstrumentId, g.Symbol], spot: float, lots: int, today: date):
    """(long put, short put) market orders for the strategy's spread on the nearest NIFTY expiry."""
    puts = [i for i in symbols if i.exchange == "NSE" and i.underlying == "NIFTY" and i.kind.value == "PE" and i.expiry >= today]
    expiry = min(i.expiry for i in puts)
    chain = {i.strike: i for i in puts if i.expiry == expiry}
    pick = lambda pct: chain[min(chain, key=lambda k: abs(k - spot * (1 - pct / 100)))]
    short, long_ = pick(2.5), pick(4.5)
    mk = lambda inst, side: Order(inst, side, lots * symbols[inst].lot_size, OrderType.MARKET, Product.CARRY, "smoke", "smoke")
    return mk(long_, Side.BUY), mk(short, Side.SELL)


def _keys(x) -> str:
    return f"list of {len(x)}" if isinstance(x, list) else f"keys {sorted(x)}" if isinstance(x, dict) else repr(type(x))


def smoke(api, symbols: dict[InstrumentId, g.Symbol], spot: float, lots: int, today: date,
          prices: dict | None = None) -> list[str]:
    ro = ReadOnlyApi(api)
    ex = GrowwExecution(ro, symbols, armed=False)
    out, notes = [], []

    def section(title, fn):
        out.append(f"\n== {title}")
        try:
            fn()
        except Exception as e:
            out.append(f"  FAILED: {type(e).__name__}: {e}")
            notes.append(f"{title}: {type(e).__name__}: {e}")

    def positions():
        raw = ro.get_positions_for_user(segment=g.SEGMENT)
        out.append(f"  raw response: {_keys(raw)}")
        rows = raw.get("positions", []) if isinstance(raw, dict) else raw
        for r in rows:
            out.append("  row: " + ", ".join(f"{k}={v}" for k, v in sorted(r.items()) if v not in (0, 0.0, None, "")))
        parsed = ex.positions()
        out.append(f"  parsed by the adapter: {[(str(p.instrument), p.qty) for p in parsed] or 'no open positions'}")
        if not rows:
            notes.append("positions: none open, so how overnight positions are reported is still unchecked")

    def margin():
        raw = ro.get_available_margin_details()
        out.append(f"  raw response: {_keys(raw)}")
        m = g.parse_available(raw)
        out.append(f"  clear cash Rs {m.clear_cash:,.2f}; option-selling balance Rs {m.option_sell:,.2f}; "
                   f"option-buying balance Rs {m.option_buy:,.2f}; margin in use Rs {m.net_used:,.2f}")

    def orders():
        raw = ro.get_order_list(segment=g.SEGMENT, page=0)
        out.append(f"  raw response: {_keys(raw)}")
        rows = ex._all_orders()
        out.append(f"  today's F&O orders: {len(rows)}; statuses {sorted({r.get('order_status') for r in rows}) or '-'}")
        if rows:
            out.append(f"  order fields: {sorted(rows[0])}")
        untagged = ex.untagged_orders()
        out.append(f"  not placed by the bot (no 'tl' reference): {len(untagged)}")
        if untagged:
            notes.append(f"orders: {len(untagged)} order(s) today were not placed by the bot")

    def spread():
        long_o, short_o = spread_orders(symbols, spot, lots, today)
        sl, ss = symbols[long_o.instrument], symbols[short_o.instrument]
        out.append(f"  spread: sell {ss.trading_symbol} / buy {sl.trading_symbol}, {long_o.qty} units (spot {spot:,.2f})")
        px = prices if prices is not None else last_prices([long_o.instrument, short_o.instrument])
        out.append("  reference prices (last recorded): " + ", ".join(f"{symbols[i].trading_symbol} {px.get(i, 'none')}"
                                                                    for i in (long_o.instrument, short_o.instrument)))
        if len(px) < 2:
            notes.append("margin: no recorded price for one leg, so its premium is counted as 0")
        results = {}
        for name, legs in (("short put alone", [(short_o, ss)]), ("the spread (hedged basket)", [(long_o, sl), (short_o, ss)])):
            raw = ro.get_order_margin_details(**g.margin_request(legs, px))
            m = g.parse_margin(raw)
            results[name] = m
            out.append(f"  {name}: total Rs {m.total:,.0f} (span {m.span:,.0f}, exposure {m.exposure:,.0f}, "
                       f"premium {m.option_premium:,.0f}, charges {m.charges:,.0f}); raw {_keys(raw)}")
        alone, basket = results["short put alone"].total, results["the spread (hedged basket)"].total
        out.append(f"  hedge benefit: Rs {alone - basket:,.0f} less margin with the protection bought")
        if basket >= alone:
            notes.append("margin: the basket shows no hedge benefit; check how Groww margins spreads")

    section("Positions", positions)
    section("Available margin", margin)
    section("Today's orders", orders)
    section("Margin for a hypothetical put credit spread (a quote, nothing is sent)", spread)
    out.append("\n== Needs attention" if notes else "\n== Nothing unexpected")
    out += [f"  - {n}" for n in notes]
    return out


def main():
    from tradeloop.auth import login
    from tradeloop.brokers.groww import INSTRUMENT_CSV_URL

    p = argparse.ArgumentParser()
    p.add_argument("--spot", type=float, default=None, help="NIFTY level for the hypothetical spread (default: last recorded)")
    p.add_argument("--lots", type=int, default=1)
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    spot = a.spot or last_nifty_spot()
    if not spot:
        raise SystemExit("no NIFTY level: pass --spot")
    symbols = g.symbols_from_rows(pd.read_csv(INSTRUMENT_CSV_URL, dtype={"exchange_token": str}, low_memory=False))
    print("\n".join(smoke(login(), symbols, spot, a.lots, date.today())))


if __name__ == "__main__":
    main()
