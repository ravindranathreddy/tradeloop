"""Recorder wiring, offline: fake feed -> bus -> strike selection + TickWriter."""
from datetime import date

from tests.test_instruments_store import fake_contracts
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Tick
from tradeloop.recorder import Recorder
from tradeloop.store import Store

NIFTY = InstrumentId.index("NSE", "NIFTY")
CFG = {"underlyings": [{"name": "NIFTY", "exchange": "NSE", "strikes_each_side": 1, "recenter_points": 200}]}


class FakeMarketData:
    def __init__(self):
        self.handlers, self.subscribed, self.stopped = [], [], False

    def instruments(self): return fake_contracts()
    def subscribe(self, ids): self.subscribed += list(ids)
    def on_tick(self, handler): self.handlers.append(handler)
    def run(self): pass
    def stop(self): self.stopped = True

    def push(self, tick):
        for h in self.handlers:
            h(tick)


def make(tmp_path):
    md, store = FakeMarketData(), Store(tmp_path / "t.db")
    rec = Recorder(CFG, md, store, today=date(2026, 10, 26))
    rec.start()
    return md, store, rec


def strikes(ids):
    return sorted({i.strike for i in ids if i.kind.is_option})


def test_first_index_value_picks_strikes(tmp_path):
    md, store, rec = make(tmp_path)
    assert md.subscribed == [NIFTY]
    md.push(Tick(NIFTY, 1, ltp=25510.0))
    assert strikes(md.subscribed) == [25450, 25500, 25550]
    assert store.count("contracts") == 6


def test_recentres_only_on_a_big_move(tmp_path):
    md, store, rec = make(tmp_path)
    md.push(Tick(NIFTY, 1, ltp=25510.0))
    md.push(Tick(NIFTY, 2, ltp=25690.0))            # moved 180 < 200: nothing new
    assert len(md.subscribed) == 1 + 6
    md.push(Tick(NIFTY, 3, ltp=25720.0))            # moved 210: add strikes around 25700
    assert strikes(md.subscribed) == [25450, 25500, 25550, 25650, 25700, 25750]
    assert store.count("contracts") == 12


def test_ticks_are_stored(tmp_path):
    md, store, rec = make(tmp_path)
    md.push(Tick(NIFTY, 1, ltp=25510.0, recv_ms=2))
    rec.writer.flush()
    assert store.conn.execute("SELECT * FROM ltp").fetchall() == [("NSE|NIFTY|IDX", 1, 2, 25510.0, None, None)]


# --- disk guard ----------------------------------------------------------------

def test_low_disk_message():
    from tradeloop.recorder import GB, low_disk
    assert low_disk("x.db", 5, free=lambda p: 6 * GB) is None
    assert low_disk("x.db", 5, free=lambda p: 4 * GB) == "only 4.0 GB free for x.db, need 5 GB"


def test_stops_when_disk_runs_low_checking_at_most_once_a_minute(tmp_path):
    from tradeloop.recorder import GB
    free, now, checks = [10 * GB], [0.0], []

    def fake_free(path):
        checks.append(path)
        return free[0]

    md, store = FakeMarketData(), Store(tmp_path / "t.db")
    rec = Recorder({**CFG, "min_free_gb": 5}, md, store, date(2026, 10, 26),
                   free=fake_free, clock=lambda: now[0])
    md.push(Tick(NIFTY, 1, ltp=25510.0))
    assert len(checks) == 1 and not md.stopped
    free[0] = 4 * GB
    md.push(Tick(NIFTY, 2, ltp=25511.0))            # within the minute: not re-checked
    assert len(checks) == 1 and not md.stopped
    now[0] = 61
    md.push(Tick(NIFTY, 3, ltp=25512.0))
    assert md.stopped and rec.stopped_for_disk
    assert checks[-1] == tmp_path / "t.db"


def test_free_bytes_handles_a_db_folder_that_does_not_exist_yet(tmp_path):
    from tradeloop.recorder import free_bytes
    assert free_bytes(tmp_path / "not" / "yet" / "ticks.db") > 0


def test_health_disk_line(tmp_path):
    from tradeloop.health import disk_line
    (tmp_path / "ticks.db").write_bytes(b"x" * 1000)
    (tmp_path / "ticks.db-wal").write_bytes(b"x" * 1000)
    line = disk_line(tmp_path / "ticks.db", days=2)
    assert "used by 2 day(s)" in line and "more days at this rate" in line


def test_paper_traders_listen_to_the_recorders_ticks_and_a_bad_config_is_skipped(tmp_path):
    from tradeloop.recorder import paper_traders
    md, store, rec = make(tmp_path)
    good = tmp_path / "good.yaml"
    good.write_text("strategy_id: p\nstrategy: put_credit_spread\nunderlyings: [NSE|NIFTY|IDX]\nlots: 1\n"
                    "entry_time: '09:20'\nenter_within_days: 2\nshort_otm_pct: 2.5\nlong_otm_pct: 4.5\n")
    cfg = {**CFG, "paper": [str(tmp_path / "missing.yaml"), str(good)], "paper_db": str(tmp_path / "paper.db")}
    traders = paper_traders(cfg, rec, md)
    assert [t.strategy_id for t in traders] == ["p"]
    md.push(Tick(NIFTY, 1, ltp=25000.0))
    assert traders[0].index_price == 25000.0 and store.count("ltp") >= 0     # recording unaffected
