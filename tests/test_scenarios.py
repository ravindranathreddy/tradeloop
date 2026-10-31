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


# --- live drills: the same days through GrowwExecution against a fake Groww exchange ----------------
# (the fake exchange fills orders against the fake feed's book; its state outlives each simulated day)

LSHORT, LLONG = "NIFTY26N0324400PE", "NIFTY26N0323900PE"


def groww_flat(r):
    return not any(r.groww()["net"].values())


def test_live_normal_week_sends_tagged_limit_orders_protection_first_and_settles(runs):
    r = runs["live_normal_week"]
    assert r.sent() == [(LLONG, "BUY", "LIMIT"), (LSHORT, "SELL", "LIMIT")]            # market orders go out as limits
    assert all(o["ref"].startswith("tl") for o in r.groww()["placed"])                  # Rule #1: every order is ours
    assert [(f[0], f[1]) for f in r.fills()] == [(LONG, "BUY"), (SHORT, "SELL")]
    assert r.order_statuses() == ["FILLED", "FILLED"] and r.kill() is None and flat(r)
    assert "ALERT LIVE ARMED" in r.log
    assert "Reconciled: ledger and broker agree on 2 live positions" in r.log          # Tuesday, against Groww's positions
    assert 0 < r.ledger().realised_pnl() < 150_000


def test_live_unarmed_decides_but_sends_nothing(runs):
    r = runs["live_unarmed"]
    assert r.sent() == [] and r.fills() == [] and r.ledger().positions() == []
    assert "ALERT LIVE not armed" in r.log and "Groww adapter is not armed" in r.log
    assert r.kill() is None


def test_live_dead_feed_flattens_at_groww_while_the_feed_is_still_dead(runs):
    r = runs["live_dead_feed"]
    assert "stale data" in r.kill()["reason"] and r.kill()["flatten"] is True
    placed = r.groww()["placed"]
    assert [(o["symbol"], o["side"]) for o in placed] == [(LLONG, "BUY"), (LSHORT, "SELL"), (LSHORT, "BUY"), (LLONG, "SELL")]
    assert all("09:40" <= o["hm"] < "09:50" for o in placed[2:])                        # exits went out during the silence
    assert groww_flat(r) and flat(r) and len(r.fills()) == 4


def test_live_manual_kill_flattens_the_account_short_first(runs):
    r = runs["live_manual_kill"]
    assert r.kill()["reason"] == "drill" and flat(r) and groww_flat(r)
    assert [f[1] for f in r.fills()[2:]] == ["BUY", "SELL"]


def test_live_startup_mismatch_at_groww_halts_without_trading_and_resume_restarts(runs):
    r = runs["live_startup_mismatch"]
    assert r.after_mismatch["flatten"] is False and "untagged" in r.after_mismatch["reason"]
    assert len(r.sent()) == 2 and len(r.fills()) == 2                                   # Tuesday sent nothing
    assert "ALERT reconciliation mismatch" in r.log
    assert r.kill() is None and "Reconciled" in r.log.split("=== 2026-11-03 ===")[-1]


def test_live_foreign_order_in_the_account_halts_the_bot_before_it_trades(runs):
    r = runs["live_foreign_order"]
    assert r.after_halt["flatten"] is False and "untagged order 'manualorder1'" in r.after_halt["reason"]
    assert r.sent() == [] and r.fills() == []
    assert "not ours (Rule #1)" in r.log


def test_live_groww_outage_trips_the_watchdog_and_the_flatten_finishes_after_it(runs):
    r = runs["live_groww_outage"]
    assert "broker unreachable" in r.kill()["reason"]
    assert r.sent()[2:] == [(LSHORT, "BUY", "LIMIT"), (LLONG, "SELL", "LIMIT")]         # exactly one exit per leg, nothing doubled
    assert all(o["hm"] >= "09:50" for o in r.groww()["placed"][2:])                     # nothing could be sent while it was down
    assert flat(r) and groww_flat(r)


def test_live_recorder_crash_is_caught_and_the_restart_flattens_the_account(runs):
    r = runs["live_recorder_crash"]
    assert r.codes[0] == 137 and "heartbeat lost" in r.alert and "kill switch requested" in r.alert
    assert flat(r) and groww_flat(r) and len(r.fills()) == 4


def test_live_lost_order_response_is_adopted_not_resent(runs):
    r = runs["live_lost_response"]
    assert r.sent() == [(LLONG, "BUY", "LIMIT"), (LSHORT, "SELL", "LIMIT")]             # one order per leg despite the timeout
    assert [(f[0], f[1]) for f in r.fills()] == [(LONG, "BUY"), (SHORT, "SELL")]
    assert "may or may not have reached Groww" in r.log


def test_live_and_paper_run_side_by_side_without_touching_each_other(runs):
    from tradeloop.paper_store import PaperStore

    r = runs["live_and_paper_together"]
    paper = PaperStore(r.dir / "paper.db")
    assert len(r.fills()) == 2 and len(paper.query("SELECT 1 FROM fills")) == 2
    assert len(r.sent()) == 2                                                           # only the live trader reached Groww
    assert r.kill() is None
    assert "Reconciled: ledger and broker agree on 2 live positions" in r.log


def test_live_margin_too_low_for_the_short_buys_the_protection_then_sells_it_back(runs):
    r = runs["live_margin_short"]
    assert r.sent() == [(LLONG, "BUY", "LIMIT"), (LLONG, "SELL", "LIMIT")]              # no short was ever sent
    assert flat(r) and groww_flat(r) and r.kill() is None
    assert "free margin Rs 70,000 is below" in r.log and "ALERT risk rejected SELL" in r.log
    assert -r.ledger().realised_pnl() / 100 < 500                                       # the round trip costs spread + charges only


def test_live_no_margin_figure_means_no_orders_at_all(runs):
    r = runs["live_margin_unreadable"]
    assert r.sent() == [] and r.fills() == [] and r.ledger().positions() == []
    assert "margin check failed (ConnectionError" in r.log
