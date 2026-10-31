"""LiveBroker (GrowwExecution behind the paper trader's surface) against the fake Groww API."""
from pathlib import Path

import pytest

from tests.test_groww_execution import Clock, FakeGroww, placed, ref
from tests.test_groww_orders import LONG, SHORT, SYMS, order
from tradeloop.brokers.groww_execution import GrowwExecution
from tradeloop.core.types import Level, OrderStatus, OrderType, Side, Tick
from tradeloop.live_broker import LiveBroker
from tradeloop.paper import PaperTrader
from tradeloop.paper_store import PaperStore

STRATEGY = Path(__file__).resolve().parents[1] / "strategies" / "put_credit_spread.yaml"


def book(inst, bid, ask, clk):
    return Tick(inst, clk.now, ltp=(bid + ask) / 2, recv_ms=clk.now, bid=bid, ask=ask,
                bids=(Level(bid, 650.0),), asks=(Level(ask, 650.0),))


@pytest.fixture
def rig():
    api, clk = FakeGroww(), Clock()
    clk.now = 1_000_000
    ex = GrowwExecution(api, SYMS, armed=True, clock_ms=clk, sleep=lambda s: None)
    lb = LiveBroker(ex, SYMS, clock_ms=clk)
    lb.updates, lb.fills = [], []
    lb.on_order_update(lb.updates.append)
    lb.on_fill(lb.fills.append)
    lb.api, lb.clk = api, clk
    return lb


def test_market_buy_goes_out_as_a_limit_above_the_ask_on_a_tick(rig):
    rig.on_tick(book(LONG, 3.50, 3.65, rig.clk))
    rig.place(order(LONG, Side.BUY, ref="b1", otype=OrderType.MARKET))
    p = placed(rig.api)[0]
    assert (p["order_type"], p["transaction_type"], p["price"]) == ("LIMIT", "BUY", 3.85)    # 3.65 + 4 ticks


def test_market_sell_goes_out_as_a_limit_below_the_bid(rig):
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    rig.place(order(SHORT, Side.SELL, ref="s1"))
    p = placed(rig.api)[0]
    assert (p["order_type"], p["price"]) == ("LIMIT", 19.40)


def test_prices_stay_on_the_tick_and_a_sell_never_goes_below_one_tick(rig):
    rig.on_tick(book(LONG, 0.05, 0.10, rig.clk))
    rig.place(order(LONG, Side.SELL, ref="s2"))
    assert placed(rig.api)[0]["price"] == 0.05
    rig.on_tick(book(SHORT, 5.03, 5.07, rig.clk))
    rig.place(order(SHORT, Side.BUY, ref="b2"))
    assert placed(rig.api)[1]["price"] == 5.30                                                # 5.07 + 0.20 = 5.27, rounded up to the tick


def test_a_limit_the_strategy_chose_is_left_alone(rig):
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    rig.place(order(SHORT, Side.SELL, otype=OrderType.LIMIT, price=21.0, ref="l1"))
    assert placed(rig.api)[0]["price"] == 21.0


def test_no_book_at_all_sends_market(rig):
    rig.place(order(SHORT, Side.SELL, ref="m1"))
    assert placed(rig.api)[0]["order_type"] == "MARKET"


def test_a_stale_book_still_prices_the_order_but_with_a_wide_buffer(rig):
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    rig.clk.now += 120_000                                    # feed down: the kill switch must still be able to exit
    assert not rig.fresh(SHORT)
    rig.place(order(SHORT, Side.BUY, ref="k1"))
    assert placed(rig.api)[0]["price"] == 21.80               # 19.80 + 10%, on a tick


def test_fresh_follows_the_clock(rig):
    assert not rig.fresh(SHORT)
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    assert rig.fresh(SHORT)
    rig.clk.now += 11_000
    assert not rig.fresh(SHORT)


def test_ticks_poll_at_most_once_a_second_and_only_while_an_order_works(rig):
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    n = lambda: sum(1 for name, _ in rig.api.calls if name in ("order_detail", "status_by_reference"))
    base = n()
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    assert n() == base                                        # nothing working: no polling
    rig.place(order(SHORT, Side.SELL, ref="p1"))
    rig.clk.now += 1_000
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    after_first = n()
    assert after_first > base
    rig.clk.now += 300
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    assert n() == after_first                                 # within the second


def test_a_fill_reaches_the_handlers_on_the_next_poll(rig):
    rig.on_tick(book(LONG, 3.50, 3.65, rig.clk))
    rig.place(order(LONG, Side.BUY, ref="f1"))
    rig.api.fill(ref("f1"), 65, 3.7)
    rig.clk.now += 1_000
    rig.on_tick(book(LONG, 3.50, 3.65, rig.clk))
    assert [(f.side, f.qty, f.price) for f in rig.fills] == [(Side.BUY, 65, 3.7)]
    assert rig.updates[-1].status is OrderStatus.FILLED
    assert rig.working() == []


def test_an_order_unfilled_after_the_wait_is_cancelled(rig):
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    rig.place(order(SHORT, Side.SELL, ref="w1"))
    rig.clk.now += 60_000
    rig.step()
    assert [o.client_ref for o in rig.working()] == ["w1"]
    rig.clk.now += 61_000
    rig.step()                                                # asks Groww to cancel; the next poll sees it done
    rig.clk.now += 1_000
    rig.step()
    assert rig.working() == []
    assert rig.updates[-1].status is OrderStatus.CANCELLED


def test_unarmed_sends_nothing(rig):
    rig.execution.armed = False
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    rig.place(order(SHORT, Side.SELL, ref="u1"))
    assert placed(rig.api) == []
    assert rig.updates[-1].status is OrderStatus.REJECTED_RISK


# --- inside the paper trader: the same gate, kill switch and ledger -----------------------------

def trader(tmp_path, rig, **kw):
    t = PaperTrader(STRATEGY, PaperStore(tmp_path / "live.db"), clock_ms=rig.clk, broker=rig, **kw)
    return t


def test_the_gate_still_guards_live_orders(tmp_path, rig):
    t = trader(tmp_path, rig, kill_file=tmp_path / "KILL")
    t.gate.place(order(SHORT, Side.SELL, ref="g1"))           # no book yet: the gate refuses before Groww is asked
    assert placed(rig.api) == []
    rig.on_tick(book(SHORT, 19.60, 19.80, rig.clk))
    t.gate.place(order(SHORT, Side.SELL, qty=65 * 3, ref="g2"))
    assert placed(rig.api) == []                              # 3 lots > max_lots 1
    t.gate.place(order(SHORT, Side.SELL, ref="g3"))
    assert len(placed(rig.api)) == 1


def test_a_halt_blocks_live_orders_and_a_fill_lands_in_the_ledger(tmp_path, rig):
    from tradeloop import kill_switch

    kill = tmp_path / "KILL"
    rig.clk.now = 1_793_593_260_000                           # Mon 2 Nov 2026 09:21 IST: the ledger prices charges by date
    t = trader(tmp_path, rig, kill_file=kill)
    rig.on_tick(book(LONG, 3.50, 3.65, rig.clk))
    t.on_tick(book(LONG, 3.50, 3.65, rig.clk))
    t._direct.place(order(LONG, Side.BUY, ref="l1"))
    rig.api.fill(ref("l1"), 65, 3.7)
    rig.clk.now += 1_000
    t.on_tick(book(LONG, 3.50, 3.65, rig.clk))
    assert [(p.instrument, p.qty) for p in t.ledger.positions()] == [(LONG, 65)]
    kill_switch.request(kill, "test", rig.clk.now)
    rig.clk.now += 2_000
    t.on_tick(book(LONG, 3.50, 3.65, rig.clk))
    t.gate.place(order(SHORT, Side.SELL, ref="h1"))
    sent = [(p["trading_symbol"], p["transaction_type"]) for p in placed(rig.api)]
    assert (SYMS[SHORT].trading_symbol, "SELL") not in sent   # the strategy's sale is refused while halted
    assert sent[-1] == (SYMS[LONG].trading_symbol, "SELL")    # the kill switch's own exit goes straight through
