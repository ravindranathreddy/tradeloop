from tradeloop.kill_switch import active, clear, flatten, request
from tradeloop.core.types import Position
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

    def cancel_all(self):
        self.calls.append("cancel_all")

    def place(self, order):
        self.calls.append((order.side.value, order.qty, str(order.instrument)))


def test_flatten_cancels_first_and_closes_shorts_before_longs():
    ex = Recorder()
    orders = flatten(ex, [Position(LONG, 65, 5.0), Position(SHORT, -65, 20.0)], strategy_id="s",
                     config_version="v", request_ms=1)
    assert ex.calls == ["cancel_all", ("BUY", 65, str(SHORT)), ("SELL", 65, str(LONG))]
    assert [o.client_ref for o in orders] == ["kill-1-s-0", "kill-1-s-1"]
