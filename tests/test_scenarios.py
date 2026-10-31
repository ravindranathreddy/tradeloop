"""Drills: everything together on simulated days. Slow (about 2 minutes), so off by default:

    RUN_SCENARIOS=1 pytest tests/test_scenarios.py
"""
import os

import pytest

from tests.scenarios.scenarios import MON, run_all

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_SCENARIOS"), reason="slow; set RUN_SCENARIOS=1")
SHORT = "NSE|NIFTY|2026-11-03|24400|PE"
LONG = "NSE|NIFTY|2026-11-03|23900|PE"


@pytest.fixture(scope="module")
def runs():
    return run_all()


def flat(r):
    return r.ledger().positions() == []


def test_normal_week_enters_holds_overnight_and_settles_without_a_halt(runs):
    r = runs["normal_week"]
    assert [(f[0], f[1]) for f in r.fills()] == [(LONG, "BUY"), (SHORT, "SELL")]       # protection first, then the sale
    assert r.kill() is None and flat(r)
    assert 0 < r.ledger().realised_pnl() < 150_000                                     # paise: the credit, about Rs 950
    assert "Reconciled: ledger and broker agree on 2 live positions" in r.log          # Tuesday's startup
    assert r.order_statuses() == ["FILLED", "FILLED"]


def test_dead_feed_with_the_spread_open_trips_the_watchdog_and_flattens(runs):
    r = runs["dead_feed"]
    assert "stale data" in r.kill()["reason"] and r.kill()["flatten"] is True
    assert len(r.fills()) == 4 and flat(r)
    assert "ALERT watchdog: stale data" in r.log


def test_a_crash_move_trips_max_loss_and_the_loss_stays_inside_the_cap(runs):
    r = runs["crash_move"]
    assert "max loss" in r.kill()["reason"] and flat(r) and len(r.fills()) == 4
    loss = -r.ledger().realised_pnl() / 100
    assert 15_000 < loss < 32_500                                                      # below the spread's 500 x 65 cap


def test_manual_kill_from_the_command_line_flattens_a_running_recorder(runs):
    r = runs["manual_kill"]
    assert r.kill()["reason"] == "drill" and flat(r) and len(r.fills()) == 4
    assert [f[1] for f in r.fills()[2:]] == ["BUY", "SELL"]                            # short bought back before the protection is sold


def test_startup_mismatch_halts_without_flattening_and_resume_restarts_cleanly(runs):
    r = runs["startup_mismatch"]
    assert r.after_mismatch["flatten"] is False and "untagged" in r.after_mismatch["reason"]
    assert len(r.fills()) == 2 and r.ledger().positions() != []                        # nothing flattened, nothing new
    assert "ALERT reconciliation mismatch" in r.log
    assert r.kill() is None and "Reconciled" in r.log.split("=== 2026-11-03 ===")[-1]  # after resume, a clean second start


def test_recorder_crash_is_caught_by_the_heartbeat_check_and_the_restart_flattens(runs):
    r = runs["recorder_crash"]
    assert r.codes[0] == 137 and "heartbeat lost" in r.alert and "kill switch requested" in r.alert
    assert len(r.fills()) == 4 and flat(r)                                             # flattened on restart, nothing new entered


def test_no_liquidity_in_the_protection_means_no_short_is_ever_sold(runs):
    r = runs["no_protection_liquidity"]
    assert r.fills() == [] and r.ledger().positions() == []
    assert set(r.order_statuses()) == {"REJECTED_RISK"}
    assert "ALERT risk rejected" in r.log


def test_expiry_far_below_both_strikes_loses_exactly_the_capped_amount(runs):
    r = runs["expiry_far_below"]
    credit = next(f[3] for f in r.fills() if f[1] == "SELL") - next(f[3] for f in r.fills() if f[1] == "BUY")
    assert flat(r)
    assert round(-r.ledger().realised_pnl() / 100) == round((500 - credit) * 65)       # width minus the credit, per lot
