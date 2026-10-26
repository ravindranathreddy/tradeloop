"""End-to-end recorder test with only the network faked.

The real growwapi GrowwFeed runs here; just its NATS client is replaced. The test
publishes protobuf messages in Groww's wire format on Groww's topic names, so the
SDK's own parsing, our adapter, the bus, the recorder and SQLite all run for real.
The instrument CSV fixture is real rows cut from Groww's file (tokens, lot sizes).
"""
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from growwapi.groww import feed as sdk_feed
from growwapi.groww.constants import FeedConstants
from growwapi.groww.proto.stocks_socket_response_pb2 import StocksSocketResponseProtoDto

from tradeloop.brokers.groww import load_contracts
from tradeloop.recorder import build

FIXTURE = Path(__file__).parent / "fixtures" / "groww_instruments.csv"
TODAY = date(2026, 10, 26)
NIFTY_25500_CE = "51468"    # NSE token for NIFTY 27-Oct-2026 25500 CE, from the fixture


# --- fake network ----------------------------------------------------------------

class FakeNats:
    """Replaces growwapi's NatsClient: tracks topics, delivers pushed messages."""
    instances: list["FakeNats"] = []

    def __init__(self, url, token, seed, callback):
        self.callback, self.topics = callback, set()
        FakeNats.instances.append(self)

    def subscribe_topic(self, topic):
        self.topics.add(topic)
        return True

    def unsubscribe_topic(self, topic):
        self.topics.discard(topic)
        return True

    def is_subscribed(self, topic):
        return topic in self.topics

    def push(self, topic, payload: bytes):
        if topic in self.topics:          # NATS only delivers what you subscribed to
            self.callback(topic, payload)


class FakeApi:
    """Stands in for GrowwAPI: the feed only asks it for a socket token."""
    def __init__(self):
        self.tokens = 0

    def generate_socket_token(self, key_pair):
        self.tokens += 1
        return {"token": f"jwt-{self.tokens}", "subscriptionId": f"sub-{self.tokens}"}


# --- Groww wire format -------------------------------------------------------------

def index_msg(ts, value):
    m = StocksSocketResponseProtoDto()
    m.stocksLiveIndices.tsInMillis, m.stocksLiveIndices.value = ts, value
    return m.SerializeToString()


def ltp_msg(ts, ltp, volume, oi):
    m = StocksSocketResponseProtoDto()
    p = m.stockLivePrice
    p.tsInMillis, p.ltp, p.volume, p.openInterest = ts, ltp, volume, oi
    return m.SerializeToString()


def depth_msg(ts, bids, asks):
    m = StocksSocketResponseProtoDto()
    d = m.stocksMarketDepth
    d.tsInMillis = ts
    for i, (px, qty) in enumerate(bids, 1):
        d.buyBook[i].price, d.buyBook[i].qty = px, qty
    for i, (px, qty) in enumerate(asks, 1):
        d.sellBook[i].price, d.sellBook[i].qty = px, qty
    return m.SerializeToString()


def index_topic(exchange, token):
    return FeedConstants.get_live_index_topic("CASH", exchange, token).get_topic()


def ltp_topic(exchange, token):
    return FeedConstants.get_live_price_topic("FNO", exchange, token).get_topic()


def depth_topic(exchange, token):
    return FeedConstants.get_market_depth("FNO", exchange, token).get_topic()


# --- harness -------------------------------------------------------------------------

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


@pytest.fixture
def day(tmp_path, monkeypatch):
    monkeypatch.setattr(sdk_feed, "NatsClient", FakeNats)
    monkeypatch.setattr(sdk_feed.GrowwFeed, "_nats_clients", {})
    FakeNats.instances = []
    cfg = {"db_path": str(tmp_path / "ticks.db"), "poll_seconds": 0.01, "stale_seconds": 30,
           "underlyings": [
               {"name": "NIFTY", "exchange": "NSE", "strikes_each_side": 1, "recenter_points": 200},
               {"name": "SENSEX", "exchange": "BSE", "strikes_each_side": 1, "recenter_points": 600}]}
    clock = Clock()
    rec, md = build(cfg, FakeApi(), TODAY, contracts=load_contracts(str(FIXTURE)), clock=clock)
    rec.start()
    md.poll_once()                                    # connects and subscribes the indices
    return rec, md, clock


def nats():
    return FakeNats.instances[-1]


def rows(rec, sql):
    rec.writer.flush()
    return rec.store.conn.execute(sql).fetchall()


# --- tests ---------------------------------------------------------------------------

def test_index_values_pick_strikes_and_subscribe_them(day):
    rec, md, _ = day
    assert nats().topics == {index_topic("NSE", "NIFTY"), index_topic("BSE", "1")}

    nats().push(index_topic("NSE", "NIFTY"), index_msg(1_793_000_000_000, 25510.0))
    nats().push(index_topic("BSE", "1"), index_msg(1_793_000_000_000, 82020.0))
    md.poll_once()

    contracts = rows(rec, "SELECT instrument, lot_size FROM contracts ORDER BY instrument")
    assert [c for c, _ in contracts] == [
        "BSE|SENSEX|2026-10-29|81900|CE", "BSE|SENSEX|2026-10-29|81900|PE",
        "BSE|SENSEX|2026-10-29|82000|CE", "BSE|SENSEX|2026-10-29|82000|PE",
        "BSE|SENSEX|2026-10-29|82100|CE", "BSE|SENSEX|2026-10-29|82100|PE",
        "NSE|NIFTY|2026-10-27|25450|CE", "NSE|NIFTY|2026-10-27|25450|PE",
        "NSE|NIFTY|2026-10-27|25500|CE", "NSE|NIFTY|2026-10-27|25500|PE",
        "NSE|NIFTY|2026-10-27|25550|CE", "NSE|NIFTY|2026-10-27|25550|PE",
    ]                                                  # nearest expiry only; 3-Nov skipped
    assert {lot for _, lot in contracts} == {20, 65}
    assert ltp_topic("NSE", NIFTY_25500_CE) in nats().topics
    assert depth_topic("NSE", NIFTY_25500_CE) in nats().topics
    assert len(nats().topics) == 2 + 12 * 2           # indices + ltp and depth per option
    assert rows(rec, "SELECT instrument, ltp FROM ltp ORDER BY instrument") == [
        ("BSE|SENSEX|IDX", 82020.0), ("NSE|NIFTY|IDX", 25510.0)]


def test_option_prices_volume_oi_and_book_are_stored(day):
    rec, md, _ = day
    nats().push(index_topic("NSE", "NIFTY"), index_msg(1_793_000_000_000, 25510.0))
    md.poll_once()

    nats().push(ltp_topic("NSE", NIFTY_25500_CE), ltp_msg(1_793_000_001_000, 112.35, 845_000, 1_250_000))
    nats().push(depth_topic("NSE", NIFTY_25500_CE),
                depth_msg(1_793_000_001_000, bids=[(112.3, 650), (112.25, 1300)],
                          asks=[(112.4, 325), (112.45, 975)]))
    md.poll_once()
    md.poll_once()                                     # same snapshots again: no duplicates

    ce = "NSE|NIFTY|2026-10-27|25500|CE"
    assert rows(rec, f"SELECT ts_ms, ltp, volume, oi FROM ltp WHERE instrument='{ce}'") == [
        (1_793_000_001_000, 112.35, 845_000.0, 1_250_000.0)]
    assert rows(rec, f"SELECT bid_px, bid_qty, ask_px, ask_qty, book_json FROM depth WHERE instrument='{ce}'") == [
        (112.3, 650.0, 112.4, 325.0,
         '{"buy":[[112.3,650.0],[112.25,1300.0]],"sell":[[112.4,325.0],[112.45,975.0]]}')]


def test_big_index_move_adds_strikes(day):
    rec, md, _ = day
    nats().push(index_topic("NSE", "NIFTY"), index_msg(1_793_000_000_000, 25510.0))
    md.poll_once()
    nats().push(index_topic("NSE", "NIFTY"), index_msg(1_793_000_060_000, 25720.0))
    md.poll_once()
    assert ltp_topic("NSE", "51476") in nats().topics   # NIFTY 25650 CE, new around 25700
    assert len(rows(rec, "SELECT * FROM contracts")) == 6 + 2


def test_feed_drop_reconnects_resubscribes_and_keeps_recording(day):
    rec, md, clock = day
    nats().push(index_topic("NSE", "NIFTY"), index_msg(1_793_000_000_000, 25510.0))
    md.poll_once()
    old = nats()

    clock.t += 31                                      # feed goes silent past stale_seconds
    md.poll_once()
    new = nats()
    assert new is not old and new.topics == old.topics

    old.push(index_topic("NSE", "NIFTY"), index_msg(1_793_000_040_000, 25600.0))   # dead connection
    new.push(index_topic("NSE", "NIFTY"), index_msg(1_793_000_041_000, 25520.0))
    md.poll_once()
    assert rows(rec, "SELECT ltp FROM ltp WHERE instrument='NSE|NIFTY|IDX' ORDER BY ts_ms") == [
        (25510.0,), (25520.0,)]


def test_run_loop_records_until_stopped(day):
    rec, md, _ = day
    t = threading.Thread(target=md.run)
    t.start()
    try:
        nats().push(index_topic("NSE", "NIFTY"), index_msg(1_793_000_000_000, 25510.0))
        deadline = time.time() + 5
        while rec.store.count("contracts") < 6 and time.time() < deadline:   # saved directly, not via writer
            time.sleep(0.01)
    finally:
        md.stop()
        t.join(timeout=5)
    assert not t.is_alive()
    assert rows(rec, "SELECT ltp FROM ltp") == [(25510.0,)]
    assert rec.store.count("contracts") == 6
    rec.finish()
