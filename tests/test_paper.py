from datetime import date, datetime, time
from pathlib import Path

import pytest

from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST
from tradeloop.core.types import Level, Order, OrderStatus, OrderType, Product, Side, Tick
from tradeloop.paper import PaperTrader, report, settle
from tradeloop.paper_broker import PaperBroker
from tradeloop.paper_store import PaperStore
from tradeloop.risk import RiskGate

MON, EXP = date(2026, 11, 2), date(2026, 11, 3)          # NIFTY lot 65
NIFTY = InstrumentId.index("NSE", "NIFTY")
SHORT = InstrumentId.option("NSE", "NIFTY", EXP, 24400.0, "PE")   # ~2.4% below 25000
LONG = InstrumentId.option("NSE", "NIFTY", EXP, 23900.0, "PE")    # ~4.4% below
CFG = """strategy_id: pcs_paper_test
strategy: put_credit_spread
underlyings: [NSE|NIFTY|IDX]
lots: 1
entry_time: "09:20"
enter_within_days: 2
short_otm_pct: 2.5
long_otm_pct: 4.5
"""


def ms(hh, mm, ss=0, d=MON):
    return int(datetime.combine(d, time(hh, mm, ss), IST).timestamp() * 1000)


class Clock:
    def __init__(self):
        self.now = ms(9, 0)

    def __call__(self):
        return self.now


def lot(_):
    return 65


def book(iid, ts, bid, ask, qty=650.0):
    return Tick(iid, ts, recv_ms=ts, bids=(Level(bid, qty), Level(bid - 0.5, qty)),
                asks=(Level(ask, qty), Level(ask + 0.5, qty)), bid=bid, ask=ask)


def order(side=Side.BUY, qty=65, otype=OrderType.MARKET, price=None, ref="r1", iid=SHORT):
    return Order(iid, side, qty, otype, Product.CARRY, "s", "v", price, client_ref=ref)


# --- paper broker -------------------------------------------------------------------------

def test_market_buy_takes_the_asks_in_whole_lots_and_walks_the_book():
    clk = Clock()
    b = PaperBroker(lot, clock_ms=clk)
    fills = []
    b.on_fill(fills.append)
    clk.now = ms(9, 20)
    b.on_tick(Tick(SHORT, clk.now, recv_ms=clk.now, asks=(Level(20.0, 100.0), Level(20.5, 650.0))))
    b.place(order(qty=195))
    assert [(f.qty, f.price) for f in fills] == [(65, 20.0), (130, 20.5)]   # 100 at 20.0 is one lot
    b.place(order(qty=65, ref="r2"))                                          # same book: liquidity used
    assert [(f.qty, f.price) for f in fills[2:]] == [(65, 20.5)]


def test_no_fill_from_a_stale_book_and_orders_lapse():
    clk = Clock()
    b = PaperBroker(lot, clock_ms=clk, stale_ms=10_000, max_wait_minutes=5)
    fills, updates = [], []
    b.on_fill(fills.append)
    b.on_order_update(updates.append)
    b.on_tick(book(SHORT, ms(9, 20), 19.5, 20.0))
    clk.now = ms(9, 20, 30)                                                   # book is 30 s old
    b.place(order())
    assert fills == []
    b.on_tick(book(SHORT, ms(9, 20, 31), 19.5, 20.1))                         # fresh book arrives
    clk.now = ms(9, 20, 31)
    assert [(f.qty, f.price) for f in fills] == [(65, 20.1)]
    b.place(order(ref="r2", iid=LONG))                                        # no book for this one
    assert updates[-1].status is OrderStatus.OPEN
    clk.now = ms(9, 26)                                                       # past the 5-minute wait
    b.step()
    assert updates[-1].status is OrderStatus.CANCELLED and fills[-1].instrument == SHORT


def test_limit_order_takes_only_levels_within_its_price():
    clk = Clock()
    b = PaperBroker(lot, clock_ms=clk)
    fills = []
    b.on_fill(fills.append)
    clk.now = ms(9, 20)
    b.on_tick(book(SHORT, clk.now, 19.5, 20.0, qty=65.0))
    b.place(order(qty=130, otype=OrderType.LIMIT, price=20.0))
    assert [(f.qty, f.price) for f in fills] == [(65, 20.0)]                  # 20.5 is beyond the limit


def test_risk_gate_rejects_big_orders_and_stale_data():
    clk = Clock()
    b = PaperBroker(lot, clock_ms=clk)
    g = RiskGate(b, max_lots=1, lot_size=lot, fresh=b.fresh, clock_ms=clk)
    updates = []
    g.on_order_update(updates.append)
    g.place(order(qty=130))
    g.place(order(ref="r2"))                                                  # no book at all
    assert [u.status for u in updates] == [OrderStatus.REJECTED_RISK] * 2
    assert "exceeds" in updates[0].message and "stale" in updates[1].message


# --- paper trader, end to end ---------------------------------------------------------

def monday_ticks(minutes, d=MON):
    """Index at 25000 and both puts quoted with a book, twice a minute (live is many times a second)."""
    for hh, mm in minutes:
        for sec in (1, 5):
            ts = ms(hh, mm, sec, d)
            yield Tick(NIFTY, ts, ltp=25000.0, recv_ms=ts)
            yield Tick(SHORT, ts, ltp=20.0, recv_ms=ts)
            yield Tick(LONG, ts, ltp=5.0, recv_ms=ts)
            yield book(SHORT, ts, 19.8, 20.2)
            yield book(LONG, ts, 4.9, 5.1)


def feed(trader, clk, ticks):
    for t in ticks:
        clk.now = t.recv_ms
        trader.on_tick(t)


@pytest.fixture
def setup(tmp_path):
    cfg_path = tmp_path / "pcs.yaml"
    cfg_path.write_text(CFG)
    return cfg_path, PaperStore(tmp_path / "paper.db"), Clock()


def test_enters_the_spread_at_the_live_book_protection_first(setup):
    cfg_path, store, clk = setup
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk)
    feed(t, clk, monday_ticks([(9, m) for m in range(17, 24)]))
    fills = store.query("SELECT instrument, side, qty, price FROM fills ORDER BY ts_ms")
    assert fills == [(str(LONG), "BUY", 65, 5.1), (str(SHORT), "SELL", 65, 19.8)]   # ask, then bid
    assert {p.instrument: p.qty for p in t.ledger.positions()} == {LONG: 65, SHORT: -65}
    assert store.load_state("pcs_paper_test", EXP)["state"] == "holding"
    assert store.query("SELECT status FROM orders") == [("FILLED",), ("FILLED",)]


def test_a_restart_keeps_positions_and_does_not_trade_again(setup):
    cfg_path, store, clk = setup
    feed(PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk), clk, monday_ticks([(9, m) for m in range(17, 24)]))
    again = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk)              # next morning's process
    assert {p.instrument: p.qty for p in again.ledger.positions()} == {LONG: 65, SHORT: -65}
    feed(again, clk, monday_ticks([(9, m) for m in range(17, 30)], d=EXP))
    assert len(store.query("SELECT * FROM fills")) == 2


class FakeApi:
    def __init__(self, close):
        self.close = close

    def get_historical_candles(self, exchange, segment, symbol, start, end, interval, timeout=None):
        return {"candles": [[f"{EXP}T00:00:00", 25000, 25100, 24000, self.close]]}


def test_settle_at_the_official_close_and_report(setup):
    cfg_path, store, clk = setup
    feed(PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk), clk, monday_ticks([(9, m) for m in range(17, 24)]))
    cfg = {"paper": [str(cfg_path)]}
    out = settle(cfg, store, FakeApi(24100.0), EXP)                                 # close between the strikes
    assert "settled 2 positions at the official close 24100.00" in out[0]
    ledger = store.rebuild_ledger("pcs_paper_test")
    assert ledger.positions() == []
    # sold at 19.8, bought at 5.1: credit 14.7; short pays 300 at 24100, long pays 0
    assert ledger.realised_pnl() == round((14.7 - 300) * 65 * 100)
    text = report(cfg, store)
    assert "settled weeks 1" in text and "orders: FILLED 2" in text


def test_decides_only_once_books_have_refreshed_in_the_new_minute(setup):
    cfg_path, store, clk = setup
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk)
    ticks = list(monday_ticks([(9, m) for m in range(17, 21)]))
    first_of_0920 = next(i for i, x in enumerate(ticks) if x.ts_ms >= ms(9, 20))
    feed(t, clk, ticks[:first_of_0920 + 1])          # only the 09:20:01 index tick so far
    assert store.query("SELECT * FROM orders") == []
    feed(t, clk, ticks[first_of_0920 + 1:])          # 09:20:05: books fresh, decide
    assert store.query("SELECT side, status FROM orders") == [("BUY", "FILLED")]
