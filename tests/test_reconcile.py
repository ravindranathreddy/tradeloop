from datetime import date, timedelta

from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Position
from tradeloop.kill_switch import active
from tradeloop.reconcile import compare, startup_check
from tests.test_paper import EXP, LONG, SHORT, PaperTrader, feed, lot, monday_ticks, ms, setup  # noqa: F401

TODAY = date(2026, 11, 2)
OTHER = InstrumentId.option("NSE", "NIFTY", EXP, 20000.0, "PE")


def pos(i, q):
    return Position(i, q, 1.0)


def test_agreement_is_clean():
    book = [pos(LONG, 65), pos(SHORT, -65)]
    assert compare(book, list(reversed(book)), TODAY) == ([], [])


def test_the_three_kinds_of_mismatch():
    got, _ = compare([pos(LONG, 65), pos(SHORT, -65)], [pos(LONG, 130), pos(OTHER, 65)], TODAY)
    assert {(m.kind, m.instrument) for m in got} == {("quantity", LONG), ("missing", SHORT), ("untagged", OTHER)}
    assert str(next(m for m in got if m.kind == "untagged")) == f"untagged: {OTHER} ledger +0, broker +65"


def test_quantities_add_up_across_strategies():
    assert compare([pos(LONG, 65), pos(LONG, 65)], [pos(LONG, 130)], TODAY) == ([], [])


def test_expired_contracts_are_not_a_mismatch_but_are_reported():
    after = EXP + timedelta(days=1)                       # morning after expiry; broker already dropped them
    got, awaiting = compare([pos(LONG, 65), pos(SHORT, -65)], [], after)
    assert got == [] and awaiting == sorted([LONG, SHORT], key=str)


def test_a_restarted_paper_broker_remembers_the_open_spread(setup, tmp_path):
    cfg_path, store, clk = setup
    feed(PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk), clk, monday_ticks([(9, m) for m in range(17, 24)]))
    again = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk)              # next morning's process
    assert {p.instrument: p.qty for p in again.broker.positions()} == {LONG: 65, SHORT: -65}
    assert startup_check([again], tmp_path / "KILL", TODAY) == []
    assert active(tmp_path / "KILL") is None


def test_a_mismatch_halts_everything_but_leaves_the_positions_alone(setup, tmp_path):
    cfg_path, store, clk = setup
    kf = tmp_path / "KILL"
    feed(PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk), clk, monday_ticks([(9, m) for m in range(17, 24)]))
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk, kill_file=kf)
    t.broker.load_positions({LONG: 65, SHORT: -65, OTHER: 65})                    # a position the ledger never booked
    clk.now = ms(9, 24)
    got = startup_check([t], kf, TODAY, now_ms=clk.now)
    assert [m.kind for m in got] == ["untagged"]
    req = active(kf)
    assert req["flatten"] is False and "reconciliation" in req["reason"]
    feed(t, clk, monday_ticks([(9, 25)]))
    assert t.halted
    assert {p.instrument: p.qty for p in t.ledger.positions()} == {LONG: 65, SHORT: -65}   # nothing flattened
    assert len(store.query("SELECT * FROM fills")) == 2


def test_an_existing_halt_is_not_overwritten(setup, tmp_path):
    from tradeloop.kill_switch import request
    cfg_path, store, clk = setup
    kf = tmp_path / "KILL"
    request(kf, "manual drill", now_ms=1)
    t = PaperTrader(cfg_path, store, lot_size=lot, clock_ms=clk, kill_file=kf)
    t.broker.load_positions({OTHER: 65})
    startup_check([t], kf, TODAY)
    assert active(kf)["reason"] == "manual drill"
