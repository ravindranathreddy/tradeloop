"""Live wiring: recorder setup, startup checks against Groww, the watchdog driving a kill with a dead
feed, and the standalone flatten. All against the fake Groww API; no network, no real orders."""
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_groww_execution import Clock, FakeGroww, placed, ref
from tests.test_groww_orders import LONG, SHORT, SYMS, order
from tests.test_live_broker import STRATEGY, book
from tradeloop import kill_switch, recorder
from tradeloop.brokers.groww_execution import GrowwExecution
from tradeloop.core.types import Side
from tradeloop.live_broker import LiveBroker
from tradeloop.live_kill import flatten_live, main as flatten_main, recorder_running
from tradeloop.paper import PaperTrader
from tradeloop.paper_store import PaperStore
from tradeloop.reconcile import startup_check
from tradeloop.watchdog import Watchdog

MON_0921 = 1_793_593_260_000                  # Mon 2 Nov 2026 09:21 IST, in session
SHORT_SYM, LONG_SYM = SYMS[SHORT].trading_symbol, SYMS[LONG].trading_symbol


class AutoFill(FakeGroww):
    """Every order fills at once at its limit price (or 10)."""

    def place_order(self, **kw):
        resp = super().place_order(**kw)
        self.fill(kw["order_reference_id"], kw["quantity"], kw.get("price") or 10.0)
        return resp


def make_trader(tmp_path, api=None, clk=None, armed=True):
    api, clk = api or AutoFill(), clk or Clock()
    clk.now = MON_0921
    ex = GrowwExecution(api, SYMS, armed=armed, clock_ms=clk, sleep=lambda s: None)
    lb = LiveBroker(ex, SYMS, clock_ms=clk)
    t = PaperTrader(STRATEGY, PaperStore(tmp_path / "live.db"), clock_ms=clk, broker=lb, kill_file=tmp_path / "KILL")
    lb.on_tick(book(SHORT, 19.60, 19.80, clk))
    lb.on_tick(book(LONG, 3.50, 3.65, clk))
    return t, api, clk


def hold_spread(t, clk):
    t._direct.place(order(LONG, Side.BUY, ref="e1"))
    clk.now += 1_100
    t.tend()
    t._direct.place(order(SHORT, Side.SELL, ref="e2"))
    clk.now += 1_100
    t.tend()
    assert {p.instrument for p in t.ledger.positions()} == {LONG, SHORT}


# --- recorder.live_traders ------------------------------------------------------------------------

def rec_stub():
    subs = []
    return SimpleNamespace(bus=SimpleNamespace(subscribe=lambda topic, h: subs.append(h))), subs


def cfg_for(tmp_path, **live):
    return {"kill_file": str(tmp_path / "KILL"),
            "live": {"enabled": True, "armed": False, "strategies": [str(STRATEGY)], "db": str(tmp_path / "live.db"), **live}}


def test_live_is_off_unless_enabled(tmp_path, monkeypatch):
    rec, subs = rec_stub()
    cfg = cfg_for(tmp_path)
    cfg["live"]["enabled"] = False
    assert recorder.live_traders(cfg, rec, None, FakeGroww()) == [] and subs == []
    assert recorder.live_traders({"kill_file": "x"}, rec, None, FakeGroww()) == []


def test_enabled_but_not_armed_builds_a_trader_that_refuses_every_order(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(recorder, "load_symbols", lambda api: SYMS)
    rec, subs = rec_stub()
    api = FakeGroww()
    with caplog.at_level(logging.WARNING):
        (t,) = recorder.live_traders(cfg_for(tmp_path), rec, None, api)
    assert subs == [t.on_tick] and t.broker.armed is False
    assert "ALERT LIVE not armed" in caplog.text
    t.broker.on_tick(book(SHORT, 19.60, 19.80, Clock()))
    t._direct.place(order(SHORT, Side.SELL, ref="x1"))
    assert placed(api) == []


def test_armed_only_when_the_config_says_exactly_true_and_it_is_announced(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(recorder, "load_symbols", lambda api: SYMS)
    for value, expected in ((True, True), ("true", False), ("yes", False), (1, False)):
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            (t,) = recorder.live_traders(cfg_for(tmp_path, armed=value), rec_stub()[0], None, FakeGroww())
        assert t.broker.armed is expected
        assert ("ARMED, REAL ORDERS WILL BE SENT" in caplog.text) is expected


def test_a_setup_failure_alerts_and_never_raises(tmp_path, monkeypatch, caplog):
    def boom(api):
        raise OSError("no network")
    monkeypatch.setattr(recorder, "load_symbols", boom)
    with caplog.at_level(logging.ERROR):
        assert recorder.live_traders(cfg_for(tmp_path), rec_stub()[0], None, FakeGroww()) == []
    assert "ALERT live trading not started" in caplog.text
    monkeypatch.setattr(recorder, "load_symbols", lambda api: SYMS)
    with caplog.at_level(logging.ERROR):
        cfg = cfg_for(tmp_path, strategies=[str(tmp_path / "missing.yaml")])
        assert recorder.live_traders(cfg, rec_stub()[0], None, FakeGroww()) == []
    assert "ALERT live strategy" in caplog.text


def test_orders_left_working_by_a_dead_run_are_cancelled_at_startup(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(recorder, "load_symbols", lambda api: SYMS)
    api = FakeGroww()
    old = GrowwExecution(api, SYMS, armed=True, sleep=lambda s: None)
    old.place(order(SHORT, Side.SELL, ref="old1"))                  # sent by the run that then died
    api._create({"order_reference_id": "manualorder1", "quantity": 65, "trading_symbol": SHORT_SYM, "transaction_type": "BUY"})
    with caplog.at_level(logging.ERROR):
        recorder.live_traders(cfg_for(tmp_path), rec_stub()[0], None, api)
    mine = next(o for o in api.orders.values() if o["order_reference_id"] == ref("old1"))
    foreign = next(o for o in api.orders.values() if o["order_reference_id"] == "manualorder1")
    assert mine["order_status"] == "CANCELLED" and foreign["order_status"] == "ACKED"     # not ours: left alone
    assert "from an earlier run is still working" in caplog.text


# --- startup reconciliation against Groww -----------------------------------------------------------

def test_startup_check_agrees_when_groww_matches_the_ledger(tmp_path):
    t, api, clk = make_trader(tmp_path)
    hold_spread(t, clk)
    assert startup_check([t], tmp_path / "KILL", clk_date(clk)) == []
    assert kill_switch.active(tmp_path / "KILL") is None


def clk_date(clk):
    from datetime import datetime

    from tradeloop.core.market_calendar import IST
    return datetime.fromtimestamp(clk.now / 1000, IST).date()


def test_a_position_at_groww_the_ledger_never_booked_halts_without_flattening(tmp_path):
    t, api, clk = make_trader(tmp_path)
    api.net[SHORT_SYM] = -65
    out = startup_check([t], tmp_path / "KILL", clk_date(clk))
    req = kill_switch.active(tmp_path / "KILL")
    assert [m.kind for m in out] == ["untagged"] and req["flatten"] is False
    assert placed(api) == []


def test_an_order_that_is_not_ours_halts_the_bot(tmp_path):
    t, api, clk = make_trader(tmp_path)
    api._create({"order_reference_id": "manualorder1", "quantity": 65, "trading_symbol": SHORT_SYM, "transaction_type": "BUY"})
    startup_check([t], tmp_path / "KILL", clk_date(clk))
    req = kill_switch.active(tmp_path / "KILL")
    assert req["flatten"] is False and "untagged order 'manualorder1'" in req["reason"]


def test_our_own_orders_are_not_untagged(tmp_path):
    t, api, clk = make_trader(tmp_path)
    hold_spread(t, clk)
    assert startup_check([t], tmp_path / "KILL", clk_date(clk)) == []


def test_unreadable_orders_or_positions_halt(tmp_path):
    t, api, clk = make_trader(tmp_path)
    api.get_order_list = lambda **kw: (_ for _ in ()).throw(ConnectionError("down"))
    assert [m.kind for m in startup_check([t], tmp_path / "KILL", clk_date(clk))] == ["unreadable"]
    assert kill_switch.active(tmp_path / "KILL")["flatten"] is False
    kill_switch.clear(tmp_path / "KILL")
    (tmp_path / "b").mkdir()
    t, api, clk = make_trader(tmp_path / "b")
    api.get_positions_for_user = lambda **kw: (_ for _ in ()).throw(ConnectionError("down"))
    assert [m.kind for m in startup_check([t], tmp_path / "b" / "KILL", clk_date(clk))] == ["unreadable"]


# --- the watchdog drives a kill without ticks -----------------------------------------------------

def dog_for(t, clk, tmp_path):
    return Watchdog([t], kill_file=tmp_path / "KILL", heartbeat_file=tmp_path / "hb", clock_ms=clk, interval=1)


def test_a_kill_flattens_a_live_spread_with_the_feed_dead_short_bought_back_first(tmp_path):
    t, api, clk = make_trader(tmp_path)
    hold_spread(t, clk)
    dog = dog_for(t, clk, tmp_path)
    kill_switch.request(tmp_path / "KILL", "test", clk.now)
    for _ in range(12):                                             # no tick arrives at all
        clk.now += 1_100
        dog.run_once()
    assert t.ledger.positions() == [] and not any(api.net.values())
    exits = [(kw["trading_symbol"], kw["transaction_type"]) for kw in placed(api)[2:]]
    assert exits == [(SHORT_SYM, "BUY"), (LONG_SYM, "SELL")]


def test_three_failed_position_reads_in_a_row_trip_the_watchdog(tmp_path):
    t, api, clk = make_trader(tmp_path)
    hold_spread(t, clk)
    dog = dog_for(t, clk, tmp_path)
    assert dog.run_once() == []
    api.get_positions_for_user = lambda **kw: (_ for _ in ()).throw(ConnectionError("down"))
    out = []
    for _ in range(3):
        clk.now += 1_100
        out = dog.run_once()
    assert out and "broker unreachable" in out[0]
    assert "broker unreachable" in kill_switch.active(tmp_path / "KILL")["reason"]


def test_a_single_failed_read_is_forgiven(tmp_path):
    t, api, clk = make_trader(tmp_path)
    dog = dog_for(t, clk, tmp_path)
    good = api.get_positions_for_user
    api.get_positions_for_user = lambda **kw: (_ for _ in ()).throw(ConnectionError("blip"))
    clk.now += 1_100
    dog.run_once()
    api.get_positions_for_user = good
    for _ in range(3):
        clk.now += 1_100
        assert dog.run_once() == []


def test_paper_traders_ignore_tend(tmp_path):
    from tradeloop.paper import PaperTrader as PT
    t = PT(STRATEGY, PaperStore(tmp_path / "p.db"))
    t.tend()                                                         # no broker.tend: nothing happens, nothing raises


# --- the standalone flatten ------------------------------------------------------------------------

def standalone(tmp_path, positions):
    api, clk = AutoFill(), Clock()
    clk.now = MON_0921
    api.net.update(positions)
    ex = GrowwExecution(api, SYMS, armed=True, clock_ms=clk, sleep=lambda s: None)
    lb = LiveBroker(ex, SYMS, clock_ms=clk)

    def sleep(s):
        clk.now += int(s * 1000)
    return api, clk, lb, sleep


def test_flatten_live_buys_the_short_back_before_selling_the_protection(tmp_path):
    api, clk, lb, sleep = standalone(tmp_path, {SHORT_SYM: -65, LONG_SYM: 65})
    left = flatten_live(lb, tmp_path / "KILL", clock_ms=clk, sleep=sleep, prices_for=lambda ps: {SHORT: 20.0, LONG: 4.0})
    assert left == [] and not any(api.net.values())
    sent = [(kw["trading_symbol"], kw["transaction_type"], kw["order_type"]) for kw in placed(api)]
    assert sent == [(SHORT_SYM, "BUY", "LIMIT"), (LONG_SYM, "SELL", "LIMIT")]
    assert placed(api)[0]["price"] == 22.0                            # last price 20 + 10%: no live book, wide buffer
    assert kill_switch.active(tmp_path / "KILL")["flatten"] is True


def test_flatten_live_with_no_prices_sends_market_and_every_order_carries_our_tag(tmp_path):
    api, clk, lb, sleep = standalone(tmp_path, {SHORT_SYM: -65})
    flatten_live(lb, tmp_path / "KILL", clock_ms=clk, sleep=sleep)
    assert placed(api)[0]["order_type"] == "MARKET"
    assert all(kw["order_reference_id"].startswith("tl") for kw in placed(api))


def test_flatten_live_gives_up_loudly_when_nothing_fills(tmp_path, caplog):
    api, clk, lb, sleep = standalone(tmp_path, {SHORT_SYM: -65})
    api.fill = lambda *a, **k: None                                   # the exchange never fills
    with caplog.at_level(logging.ERROR):
        left = flatten_live(lb, tmp_path / "KILL", clock_ms=clk, sleep=sleep, max_seconds=30)
    assert [(p.instrument, p.qty) for p in left] == [(SHORT, -65)]
    assert "ALERT live kill: still holding" in caplog.text
    assert len(placed(api)) == 1                                      # one working exit is never doubled up


def test_flatten_live_with_an_empty_account_just_halts(tmp_path):
    api, clk, lb, sleep = standalone(tmp_path, {})
    assert flatten_live(lb, tmp_path / "KILL", clock_ms=clk, sleep=sleep) == []
    assert placed(api) == []


def test_the_cli_needs_yes_and_refuses_while_the_recorder_is_alive(tmp_path, capsys):
    hb = tmp_path / "hb"
    cfg = {"watchdog": {"heartbeat_file": str(hb)}, "kill_file": str(tmp_path / "KILL")}
    assert flatten_main(cfg, confirmed=False, even_if_running=False) == 2
    import time
    hb.write_text(str(int(time.time() * 1000)))
    assert recorder_running(hb, int(time.time() * 1000))
    assert flatten_main(cfg, confirmed=True, even_if_running=False) == 2
    assert "recorder is running" in capsys.readouterr().out
    hb.write_text(str(int(time.time() * 1000) - 120_000))
    assert not recorder_running(hb, int(time.time() * 1000))
    assert not recorder_running(tmp_path / "none", int(time.time() * 1000))


# --- regression: a failing cancel must not lose the kill (found by the live_groww_outage drill) ---------

def test_a_cancel_that_fails_during_a_kill_is_retried_and_the_flatten_still_happens(tmp_path, caplog):
    t, api, clk = make_trader(tmp_path)
    hold_spread(t, clk)
    dog = dog_for(t, clk, tmp_path)
    good = api.get_order_list
    api.get_order_list = lambda **kw: (_ for _ in ()).throw(ConnectionError("down"))
    kill_switch.request(tmp_path / "KILL", "test", clk.now)
    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            clk.now += 1_100
            dog.run_once()
        assert len(placed(api)) == 2                                  # still trying to cancel: nothing flattened yet
        api.get_order_list = good                                     # Groww is back
        for _ in range(10):
            clk.now += 1_100
            dog.run_once()
    assert "could not cancel working orders (try 1)" in caplog.text
    assert t.ledger.positions() == [] and not any(api.net.values())


def test_if_cancelling_keeps_failing_the_flatten_goes_ahead_anyway(tmp_path, caplog):
    t, api, clk = make_trader(tmp_path)
    hold_spread(t, clk)
    dog = dog_for(t, clk, tmp_path)
    api.get_order_list = lambda **kw: (_ for _ in ()).throw(ConnectionError("orders endpoint broken"))
    kill_switch.request(tmp_path / "KILL", "test", clk.now)
    with caplog.at_level(logging.ERROR):
        for _ in range(14):
            clk.now += 1_100
            dog.run_once()
    assert t.ledger.positions() == [] and not any(api.net.values())   # the order endpoint is broken, the position still closes


def test_the_heartbeat_check_sees_live_positions_and_names_the_standalone_flatten(tmp_path):
    from tradeloop.watchdog import check_heartbeat

    t, api, clk = make_trader(tmp_path)
    hold_spread(t, clk)                                               # booked in tmp_path/live.db
    cfg = {"paper": [], "paper_db": str(tmp_path / "paper.db"), "kill_file": str(tmp_path / "KILL"),
           "watchdog": {"heartbeat_file": str(tmp_path / "hb")},
           "live": {"enabled": True, "db": str(tmp_path / "live.db"), "strategies": [str(STRATEGY)]}}
    alert = check_heartbeat(cfg, now_ms=clk.now)                      # no heartbeat file: the recorder is not running
    assert "kill switch requested" in alert and "flatten-live --yes" in alert
    assert kill_switch.active(tmp_path / "KILL")["flatten"] is True
    cfg["live"]["enabled"] = False
    kill_switch.clear(tmp_path / "KILL")
    assert "kill switch requested" not in check_heartbeat(cfg, now_ms=clk.now)       # live off: only paper positions count


def test_the_committed_config_never_sends_real_orders():
    """Arming is Ranga's decision, made on the machine that trades. If this fails because he armed it on
    purpose, that is the moment to double-check LIVE_CHECKLIST.md, then update this test."""
    import yaml

    live = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())["live"]
    assert live["enabled"] is False and live["armed"] is False
