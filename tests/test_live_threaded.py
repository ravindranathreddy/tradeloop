"""LiveBroker with its worker thread, against a fake Groww that can hang on demand."""
import logging
import threading
import time

import pytest

from tests.test_groww_execution import Clock, placed, ref
from tests.test_groww_orders import LONG, SHORT, SYMS, order
from tests.test_live_broker import STRATEGY, book
from tests.test_live_wiring import MON_0921, SHORT_SYM, LONG_SYM, AutoFill, dog_for
from tradeloop import kill_switch
from tradeloop.brokers.groww_execution import GrowwExecution
from tradeloop.core.types import OrderStatus, Side
from tradeloop.live_broker import LiveBroker
from tradeloop.paper import PaperTrader
from tradeloop.paper_store import PaperStore


class Slow(AutoFill):
    """Orders fill at once, but place_order can be made to hang until released."""

    def __init__(self):
        super().__init__()
        self.block, self.entered, self.release = False, threading.Event(), threading.Event()
        self.threads = {}

    def place_order(self, **kw):
        self.threads["place"] = threading.get_ident()
        if self.block:
            self.entered.set()
            self.release.wait(10)
        return super().place_order(**kw)

    def get_order_detail(self, **kw):
        self.threads["poll"] = threading.get_ident()
        return super().get_order_detail(**kw)


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def rig(tmp_path):
    api, clk = Slow(), Clock()
    clk.now = MON_0921
    ex = GrowwExecution(api, SYMS, armed=True, clock_ms=clk, sleep=lambda s: None)
    lb = LiveBroker(ex, SYMS, clock_ms=clk, threaded=True, sleep=lambda s: None)
    t = PaperTrader(STRATEGY, PaperStore(tmp_path / "live.db"), clock_ms=clk, broker=lb, kill_file=tmp_path / "KILL")
    lb.on_tick(book(SHORT, 19.60, 19.80, clk))
    lb.on_tick(book(LONG, 3.50, 3.65, clk))
    rig = type("Rig", (), {})()
    rig.api, rig.clk, rig.lb, rig.t, rig.tmp = api, clk, lb, t, tmp_path
    yield rig
    api.release.set()
    lb.close()


def hold_spread(r):
    for inst, side, name in ((LONG, Side.BUY, "e1"), (SHORT, Side.SELL, "e2")):
        r.t._direct.place(order(inst, side, ref=name))
        assert r.lb.join()
        r.clk.now += 1_100
        assert wait_for(lambda: (r.t.tend(), r.lb.working() == [])[1])
    assert {p.instrument for p in r.t.ledger.positions()} == {LONG, SHORT}


def test_a_hung_groww_call_never_blocks_the_tick_thread(rig):
    rig.api.block = True
    started = time.monotonic()
    rig.t.gate.place(order(LONG, Side.BUY, ref="h1"))
    assert rig.api.entered.wait(2)                                    # the worker is inside the hung call
    rig.t.on_tick(book(LONG, 3.50, 3.65, rig.clk))
    rig.t.tend()
    assert time.monotonic() - started < 1.0                           # the caller never waited for Groww
    assert [o.client_ref for o in rig.lb.working()] == ["h1"]         # in flight, and the trader knows it
    rig.api.release.set()
    assert rig.lb.join()
    assert wait_for(lambda: (rig.t.tend(), rig.lb.working() == [])[1])
    assert [(p.instrument, p.qty) for p in rig.t.ledger.positions()] == [(LONG, 65)]


def test_handlers_run_on_the_callers_thread_never_on_the_worker(rig):
    seen = []
    rig.lb.on_fill(lambda f: seen.append(threading.get_ident()))
    rig.lb.on_order_update(lambda u: seen.append(threading.get_ident()))
    rig.t.gate.place(order(LONG, Side.BUY, ref="t1"))
    assert rig.lb.join()
    assert seen == []                                                 # nothing is delivered until the caller drains
    rig.lb.drain()
    assert seen and set(seen) == {threading.get_ident()}
    assert rig.api.threads["place"] != threading.get_ident()          # and the Groww call itself ran elsewhere


def test_polling_runs_on_the_worker(rig):
    rig.api.fill = lambda *a, **k: None                               # leave the order working so it is polled
    rig.t.gate.place(order(LONG, Side.BUY, ref="p1"))
    assert rig.lb.join()
    rig.clk.now += 2_000
    assert wait_for(lambda: "poll" in rig.api.threads)
    assert rig.api.threads["poll"] != threading.get_ident()


def test_working_and_the_ledger_change_together_so_a_kill_cannot_double_exit(rig):
    rig.t._direct.place(order(LONG, Side.BUY, ref="r1"))
    assert rig.lb.join()                                              # Groww has filled it; nobody has drained yet
    assert rig.t.ledger.positions() == []
    assert [o.client_ref for o in rig.lb.working()] == ["r1"]         # still working in the trader's view, as the ledger is empty
    rig.t.tend()
    assert [p.instrument for p in rig.t.ledger.positions()] == [LONG] and rig.lb.working() == []


def test_a_kill_driven_by_rapid_tends_sends_exactly_one_exit_per_leg(rig):
    hold_spread(rig)
    kill_switch.request(rig.tmp / "KILL", "test", rig.clk.now)
    for _ in range(150):                                              # the caller spins far faster than Groww answers
        rig.clk.now += 1_100
        rig.t.tend()
        time.sleep(0.003)
    assert rig.lb.join()
    rig.t.tend()
    exits = [(kw["trading_symbol"], kw["transaction_type"]) for kw in placed(rig.api)[2:]]
    assert exits == [(SHORT_SYM, "BUY"), (LONG_SYM, "SELL")]          # never a second buy-back, never a flipped position
    assert rig.t.ledger.positions() == [] and not any(rig.api.net.values())


def test_cancel_all_is_retried_in_place_and_runs_before_the_exits_queued_after_it(rig):
    hold_spread(rig)
    real, failures = rig.api.get_order_list, []

    def flaky(**kw):
        if len(failures) < 2:
            failures.append(1)
            raise ConnectionError("down")
        return real(**kw)
    rig.api.get_order_list = flaky
    kill_switch.request(rig.tmp / "KILL", "test", rig.clk.now)
    for _ in range(60):
        rig.clk.now += 1_100
        rig.t.tend()
        time.sleep(0.01)
    assert rig.lb.join() and len(failures) == 2
    names = [n for n, _ in rig.api.calls]
    first_exit = next(i for i, (n, kw) in enumerate(rig.api.calls) if n == "place_order" and kw["order_reference_id"].startswith("tl") and i > 4
                      and kw["transaction_type"] == "BUY")
    assert "order_list" in names[:first_exit]                         # cancel-all ran before the first exit order
    rig.t.tend()
    assert rig.t.ledger.positions() == []


def test_a_bad_command_is_reported_and_the_worker_carries_on(rig, caplog):
    real = rig.lb.execution.place
    calls = []

    def once_bad(o):
        calls.append(o.client_ref)
        if len(calls) == 1:
            raise RuntimeError("bug")
        return real(o)
    rig.lb.execution.place = once_bad
    updates = []
    rig.lb.on_order_update(updates.append)
    with caplog.at_level(logging.ERROR):
        rig.t.gate.place(order(LONG, Side.BUY, ref="b1"))
        rig.t.gate.place(order(SHORT, Side.SELL, ref="b2"))
        assert rig.lb.join()
        rig.lb.drain()
    assert "ALERT live broker: place failed" in caplog.text
    bad = next(u for u in updates if u.client_ref == "b1" and u.status is OrderStatus.REJECTED)
    assert "internal error: RuntimeError" in bad.message
    assert [o.client_ref for o in rig.lb.working()] == [] and len(placed(rig.api)) == 1   # b2 went through


def test_the_same_order_twice_is_one_order_and_does_not_stick(rig):
    rig.t._direct.place(order(LONG, Side.BUY, ref="d1"))
    rig.t._direct.place(order(LONG, Side.BUY, ref="d1"))
    assert rig.lb.join()
    rig.t.tend()
    assert len(placed(rig.api)) == 1 and rig.lb.working() == []


def test_close_finishes_queued_work_then_stops_the_worker(rig):
    rig.t.gate.place(order(LONG, Side.BUY, ref="c1"))
    rig.lb.close()
    assert len(placed(rig.api)) == 1                                  # it was sent before the worker stopped
    assert not rig.lb._thread.is_alive()
    assert [p.instrument for p in rig.t.ledger.positions()] == [LONG]


def test_unarmed_threaded_sends_nothing_and_the_refusal_clears_working(rig):
    rig.lb.execution.armed = False
    rig.t._direct.place(order(SHORT, Side.SELL, ref="u1"))
    assert rig.lb.join()
    rig.t.tend()
    assert placed(rig.api) == [] and rig.lb.working() == []


def test_a_kill_with_the_feed_dead_and_a_threaded_broker_flattens_via_the_watchdog(rig):
    hold_spread(rig)
    dog = dog_for(rig.t, rig.clk, rig.tmp)
    kill_switch.request(rig.tmp / "KILL", "test", rig.clk.now)
    for _ in range(80):                                               # no tick at all
        rig.clk.now += 1_100
        dog.run_once()
        time.sleep(0.01)
    assert rig.lb.join()
    dog.run_once()
    assert rig.t.ledger.positions() == [] and not any(rig.api.net.values())
    assert [(kw["trading_symbol"], kw["transaction_type"]) for kw in placed(rig.api)[2:]] == [(SHORT_SYM, "BUY"), (LONG_SYM, "SELL")]
