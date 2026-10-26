from datetime import date

from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Contract, Level, Tick
from tradeloop.instruments import pick_options, strike_step
from tradeloop.store import Store, TickWriter


def fake_contracts():
    out = []
    for expiry in (date(2026, 10, 27), date(2026, 11, 3)):
        for strike in range(25000, 26050, 50):
            for kind in ("CE", "PE"):
                out.append(Contract(InstrumentId.option("NSE", "NIFTY", expiry, strike, kind), 75))
    out.append(Contract(InstrumentId.option("BSE", "SENSEX", date(2026, 10, 29), 82000, "CE"), 20))
    return out


def test_strike_step():
    assert strike_step(fake_contracts()[:40]) == 50


def test_pick_nearest_expiry_around_atm():
    picked = pick_options(fake_contracts(), "NSE", "NIFTY", spot=25512, each_side=2,
                          today=date(2026, 10, 26))
    assert {c.instrument.expiry for c in picked} == {date(2026, 10, 27)}
    assert sorted({c.instrument.strike for c in picked}) == [25400, 25450, 25500, 25550, 25600]
    assert len(picked) == 10  # CE + PE per strike


def test_expiry_day_still_picks_today():
    picked = pick_options(fake_contracts(), "NSE", "NIFTY", 25500, 1, today=date(2026, 10, 27))
    assert {c.instrument.expiry for c in picked} == {date(2026, 10, 27)}


def test_no_contracts_after_last_expiry():
    assert pick_options(fake_contracts(), "NSE", "NIFTY", 25500, 1, today=date(2026, 12, 1)) == []


CE = InstrumentId.option("NSE", "NIFTY", date(2026, 10, 27), 25500, "CE")


def test_store_dedupes_and_saves_contracts(tmp_path):
    s = Store(tmp_path / "t.db")
    row = (str(CE), 1, 2, 100.0, 500.0, 9000.0)
    s.write_ltp([row, row])
    s.write_ltp([row])
    assert s.count("ltp") == 1
    s.save_contracts("2026-10-26", fake_contracts()[:6])
    s.save_contracts("2026-10-26", fake_contracts()[:6])
    assert s.count("contracts") == 6
    assert s.conn.execute("SELECT instrument, lot_size FROM contracts LIMIT 1").fetchone() == \
        ("NSE|NIFTY|2026-10-27|25000|CE", 75)


def test_tick_writer_batches_and_splits_ltp_and_depth(tmp_path):
    clock = [0.0]
    s = Store(tmp_path / "t.db")
    w = TickWriter(s, max_rows=3, max_seconds=60, clock=lambda: clock[0])
    w.on_tick(Tick(CE, 1, ltp=10.0, recv_ms=5, volume=1500, oi=42000))
    w.on_tick(Tick(CE, 2, recv_ms=6, bid=9.9, bid_qty=75, ask=10.1, ask_qty=150,
                   bids=(Level(9.9, 75),), asks=(Level(10.1, 150),)))
    assert s.count("ltp") == 0                      # still buffered
    w.on_tick(Tick(CE, 3, ltp=10.5))                # third row: flush
    assert (s.count("ltp"), s.count("depth")) == (2, 1)
    w.on_tick(Tick(CE, 4, ltp=11.0))
    clock[0] = 61
    w.on_tick(Tick(CE, 5, ltp=11.5))                # time-based flush
    assert s.count("ltp") == 4
    assert s.conn.execute("SELECT * FROM depth").fetchone()[:7] == (str(CE), 2, 6, 9.9, 75, 10.1, 150)
    assert s.conn.execute("SELECT volume, oi FROM ltp WHERE ts_ms=1").fetchone() == (1500, 42000)
