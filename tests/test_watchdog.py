from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Tick
from tradeloop.engine import Quote
from tradeloop.kill_switch import active
from tradeloop.watchdog import Watchdog, check_heartbeat
from tests.test_paper import EXP, LONG, SHORT, PaperTrader, feed, lot, monday_ticks, ms, setup  # noqa: F401


def held(setup, tmp_path):
    cfg_path, store, clk = setup
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk)
    feed(t, clk, monday_ticks([(9, m) for m in range(17, 24)]))
    dog = Watchdog([t], kill_file=tmp_path / "KILL", heartbeat_file=tmp_path / "hb", stale_seconds=120, clock_ms=clk)
    return t, dog, clk


def test_quiet_when_all_is_well(setup, tmp_path):
    _, dog, clk = held(setup, tmp_path)
    clk.now = ms(9, 24)
    dog.on_tick(None)
    assert dog.run_once() == [] and active(tmp_path / "KILL") is None
    assert (tmp_path / "hb").read_text() == str(ms(9, 24))


def test_stale_data_with_positions_trips_the_kill_switch(setup, tmp_path):
    _, dog, clk = held(setup, tmp_path)
    clk.now = ms(9, 27)                                  # 3 minutes since the last tick
    assert "stale data" in dog.run_once()[0]
    assert "stale data" in active(tmp_path / "KILL")["reason"]


def test_stale_data_without_positions_is_left_to_the_risk_gate(setup, tmp_path):
    cfg_path, store, clk = setup
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk)
    dog = Watchdog([t], kill_file=tmp_path / "KILL", heartbeat_file=tmp_path / "hb", clock_ms=clk)
    clk.now = ms(10, 0)
    assert dog.run_once() == []


def test_nothing_outside_the_session(setup, tmp_path):
    _, dog, clk = held(setup, tmp_path)
    clk.now = ms(16, 30)
    assert dog.run_once() == []


def test_max_open_loss(setup, tmp_path):
    t, dog, clk = held(setup, tmp_path)
    clk.now = ms(9, 24)
    dog.on_tick(None)
    t.quotes[SHORT] = Quote(clk.now, 400.0, None, None)  # sold at 19.8: (400 - 19.8) x 65 = Rs 24.7k
    assert "max loss" in dog.run_once()[0]
    assert active(tmp_path / "KILL") is not None


def test_untagged_broker_position_needs_two_checks(setup, tmp_path):
    t, dog, clk = held(setup, tmp_path)
    clk.now = ms(9, 24)
    dog.on_tick(None)
    stray = InstrumentId.option("NSE", "NIFTY", EXP, 20000.0, "PE")
    t.broker._positions[stray] = 65                      # something the ledger never booked
    assert dog.run_once() == []
    assert "untagged position" in dog.run_once()[0]


def test_heartbeat_check_from_cron(setup, tmp_path):
    cfg_path, store, clk = setup
    held(setup, tmp_path)
    kf, hb = tmp_path / "KILL", tmp_path / "hb"
    cfg = {"paper": [str(cfg_path)], "paper_db": str(store.path), "kill_file": str(kf),
           "watchdog": {"heartbeat_file": str(hb)}}
    hb.write_text(str(ms(9, 30)))
    assert check_heartbeat(cfg, now_ms=ms(9, 30, 30)) is None                   # fresh
    assert check_heartbeat(cfg, now_ms=ms(16, 0)) is None                       # after the close
    alert = check_heartbeat(cfg, now_ms=ms(9, 35))                              # 5 minutes silent
    assert "heartbeat lost" in alert and "kill switch requested" in alert
    assert active(kf) is not None
    hb.unlink()
    kf.unlink()
    assert "kill switch requested" in check_heartbeat(cfg, now_ms=ms(9, 35))    # no heartbeat at all
