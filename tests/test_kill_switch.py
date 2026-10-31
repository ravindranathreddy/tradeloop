from tradeloop.kill_switch import active, clear, flatten_step, request
from tradeloop.core.types import Order, OrderType, Position, Product, Side
from tests.test_paper import EXP, LONG, MON, NIFTY, SHORT, PaperTrader, feed, lot, monday_ticks, ms, setup  # noqa: F401


def entered(setup, kill_file):
    cfg_path, store, clk = setup
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk, kill_file=kill_file)
    feed(t, clk, monday_ticks([(9, m) for m in range(17, 24)]))
    assert {p.instrument: p.qty for p in t.ledger.positions()} == {LONG: 65, SHORT: -65}
    return t, store, clk


def test_kill_flattens_open_positions_and_blocks_new_orders(setup, tmp_path):
    kf = tmp_path / "KILL"
    t, store, clk = entered(setup, kf)
    request(kf, "test", now_ms=clk.now)
    feed(t, clk, monday_ticks([(9, 30)]))
    assert t.ledger.positions() == []
    fills = store.query("SELECT instrument, side FROM fills ORDER BY ts_ms")
    assert fills[2:] == [(str(SHORT), "BUY"), (str(LONG), "SELL")]           # short bought back first
    assert store.query("SELECT strategy_id FROM orders WHERE client_ref LIKE 'kill-%'") == [("pcs_paper_test",)] * 2
    assert t.halted


def test_one_request_flattens_once_and_resume_lifts_the_halt(setup, tmp_path):
    kf = tmp_path / "KILL"
    t, store, clk = entered(setup, kf)
    request(kf, "test", now_ms=clk.now)
    feed(t, clk, monday_ticks([(9, 30), (9, 31)]))
    assert len(store.query("SELECT * FROM fills")) == 4                       # 2 entry + 2 flatten, no repeats
    clear(kf)
    feed(t, clk, monday_ticks([(9, 32)]))
    assert not t.halted


def test_halted_strategy_enters_nothing(setup, tmp_path):
    cfg_path, store, clk = setup
    kf = tmp_path / "KILL"
    request(kf, "before the open", now_ms=ms(9, 0))
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk, kill_file=kf)
    feed(t, clk, monday_ticks([(9, m) for m in range(17, 24)]))
    assert store.query("SELECT * FROM fills") == []
    statuses = {s for (s,) in store.query("SELECT status FROM orders")}
    assert statuses == {"REJECTED_RISK"}


def test_unreadable_kill_file_still_halts(tmp_path):
    kf = tmp_path / "KILL"
    kf.write_text("{oops")
    assert active(kf) is not None


class Recorder:
    def __init__(self):
        self.calls = []

    def place(self, order):
        self.calls.append((order.side.value, order.qty, str(order.instrument), order.client_ref))


def step(ex, positions, working=(), attempts=None):
    return flatten_step(ex, positions, list(working), strategy_id="s", config_version="v", request_ms=1,
                        attempts={} if attempts is None else attempts)


def test_shorts_are_bought_back_before_the_protection_is_sold_and_only_on_confirmed_fills():
    ex, attempts = Recorder(), {}
    step(ex, [Position(LONG, 65, 5.0), Position(SHORT, -65, 20.0)], attempts=attempts)
    assert [c[:3] for c in ex.calls] == [("BUY", 65, str(SHORT))]               # the sale of LONG waits
    step(ex, [Position(LONG, 65, 5.0)], attempts=attempts)                       # short confirmed bought back
    assert [c[:3] for c in ex.calls][1:] == [("SELL", 65, str(LONG))]


def test_an_instrument_with_a_working_order_is_left_alone_and_a_lapsed_one_is_resent_under_a_new_ref():
    ex, attempts = Recorder(), {}
    short_pos = [Position(SHORT, -65, 20.0)]
    working = [Order(SHORT, Side.BUY, 65, OrderType.MARKET, Product.CARRY, "s", "v")]
    step(ex, short_pos, attempts=attempts)
    step(ex, short_pos, working, attempts)                                       # still working: nothing new
    assert len(ex.calls) == 1
    step(ex, short_pos, attempts=attempts)                                       # lapsed unfilled: try again
    assert [c[3] for c in ex.calls] == [f"kill-1-s-{SHORT}-1", f"kill-1-s-{SHORT}-2"]


def test_protection_is_not_sold_while_the_short_cannot_fill(setup, tmp_path):
    from tradeloop.core.types import Tick
    from tests.test_paper import book
    kf = tmp_path / "KILL"
    t, store, clk = entered(setup, kf)
    request(kf, "test", now_ms=clk.now)
    only_long = []
    for sec in (1, 3, 5):                                                    # the long's book is live, the short's is not
        ts = ms(9, 30, sec)
        only_long += [Tick(NIFTY, ts, ltp=25000.0, recv_ms=ts), book(LONG, ts, 4.9, 5.1)]
    feed(t, clk, only_long)
    assert {p.instrument: p.qty for p in t.ledger.positions()} == {LONG: 65, SHORT: -65}   # nothing sold, nothing naked
    feed(t, clk, monday_ticks([(9, 31)]))                                    # the short's book returns
    assert t.ledger.positions() == []
    assert [f[1] for f in store.query("SELECT instrument, side FROM fills ORDER BY ts_ms, rowid")[2:]] == ["BUY", "SELL"]
