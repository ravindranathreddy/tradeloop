"""Kill switch, watchdog, risk gate and paper trader together: a real watchdog thread, the real
command line, files on disk. Only the clock is simulated."""
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from tradeloop.kill_switch import active
from tradeloop.watchdog import Watchdog
from tests.test_paper import CFG, LONG, SHORT, PaperTrader, feed, lot, monday_ticks, ms, setup  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


def cli(cfg_file, *args):
    return subprocess.run([sys.executable, "-m", "tradeloop.kill_switch", *args, "--config", str(cfg_file)],
                          cwd=ROOT, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def world(setup, tmp_path):
    cfg_path, store, clk = setup
    kf = tmp_path / "KILL"
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"paper": [str(cfg_path)], "paper_db": str(store.path), "kill_file": str(kf)}))
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk, kill_file=kf)
    feed(t, clk, monday_ticks([(9, m) for m in range(17, 24)]))
    return t, store, clk, kf, cfg_file


def test_manual_kill_from_the_command_line_flattens_and_resume_reopens(world):
    t, store, clk, kf, cfg_file = world
    assert "-65" in cli(cfg_file, "status") and "Not halted" in cli(cfg_file, "status")
    out = cli(cfg_file, "kill", "--reason", "drill")
    assert "Kill requested" in out and active(kf)["reason"] == "drill"
    clk.now = ms(9, 30)
    feed(t, clk, monday_ticks([(9, 30)]))
    status = cli(cfg_file, "status")
    assert "HALTED" in status and "Open paper positions: none" in status        # CLI reads what the trader wrote
    cli(cfg_file, "resume")
    assert active(kf) is None


def test_watchdog_thread_trips_on_a_dead_feed_and_the_trader_then_flattens(world):
    t, store, clk, kf, cfg_file = world
    dog = Watchdog([t], kill_file=kf, heartbeat_file=kf.parent / "hb", stale_seconds=120, interval=0.02, clock_ms=clk)
    dog.start()
    clk.now = ms(9, 27)                                  # feed silent for 3 minutes, positions open
    for _ in range(100):
        if active(kf):
            break
        time.sleep(0.02)
    assert "stale data" in active(kf)["reason"]
    clk.now = ms(9, 28)
    feed(t, clk, monday_ticks([(9, 28)]))                # feed returns: halted, positions flattened
    assert t.ledger.positions() == [] and t.halted
    assert store.query("SELECT COUNT(*) FROM fills") == [(4,)]
    assert "HALTED" in cli(cfg_file, "status")
