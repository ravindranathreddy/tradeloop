"""Groww adapter, offline. Payloads follow the Groww Python SDK Feed docs."""
import json
from datetime import date

import pandas as pd
import pytest

from tradeloop.brokers import groww
from tradeloop.brokers.groww import GrowwMarketData, contracts_from_rows
from tradeloop.core.ids import InstrumentId
from tradeloop.core.interfaces import MarketData
from tradeloop.core.types import Contract, Level

NIFTY = InstrumentId.index("NSE", "NIFTY")
SENSEX = InstrumentId.index("BSE", "SENSEX")
CE = InstrumentId.option("NSE", "NIFTY", date(2026, 10, 27), 25500, "CE")
CONTRACTS = {("NSE", "FNO", "35241"): Contract(CE, 75)}
IDS = {("NSE", "FNO", "35241"): CE, ("NSE", "CASH", "NIFTY"): NIFTY, ("BSE", "CASH", "1"): SENSEX}

LTP = {"ltp": {"NSE": {"CASH": {"2885": {"tsInMillis": 1746174479582.0, "ltp": 1419.1}},
                       "FNO": {"35241": {"tsInMillis": 1746174479582.0, "ltp": 26111.10,
                                         "volume": 1250.0, "openInterest": 98765.0}}}}}
INDEX = {"NSE": {"CASH": {"NIFTY": {"tsInMillis": 1746174582295.0, "value": 24386.7}}},
         "BSE": {"CASH": {"1": {"tsInMillis": 1746174582295.0, "value": 73386.7}}}}
DEPTH = {"NSE": {"FNO": {"35241": {
    "tsInMillis": 1746156600.0,
    "buyBook": {"2": {"price": 1420.1, "qty": 30.0}, "1": {"price": 1420.2, "qty": 120.0},
                "3": {"price": 0.0, "qty": 0.0}},
    "sellBook": {"1": {"price": 1420.4, "qty": 5.0}, "2": {"price": 1420.5, "qty": 450.0}}}}}}


# --- instrument CSV -> neutral IDs -----------------------------------------------

def test_contracts_from_csv_rows():
    df = pd.DataFrame([
        {"exchange": "NSE", "segment": "FNO", "exchange_token": "35241", "instrument_type": "CE",
         "underlying_symbol": "NIFTY", "expiry_date": "2026-10-27", "strike_price": 25500.0, "lot_size": 75},
        {"exchange": "BSE", "segment": "FNO", "exchange_token": "8812", "instrument_type": "PE",
         "underlying_symbol": "SENSEX", "expiry_date": "2026-10-29", "strike_price": 82000.0, "lot_size": 20},
        {"exchange": "NSE", "segment": "CASH", "exchange_token": "2885", "instrument_type": "EQ",
         "underlying_symbol": None, "expiry_date": None, "strike_price": None, "lot_size": 1},
        {"exchange": "NSE", "segment": "FNO", "exchange_token": "999", "instrument_type": "CE",
         "underlying_symbol": "NIFTY", "expiry_date": "garbage", "strike_price": 1.0, "lot_size": 75},
    ])
    got = contracts_from_rows(df)
    assert got == {
        ("NSE", "FNO", "35241"): Contract(CE, 75),
        ("BSE", "FNO", "8812"): Contract(InstrumentId.parse("BSE|SENSEX|2026-10-29|82000|PE"), 20),
    }  # equity skipped (not in use yet), bad expiry skipped


# --- snapshots -> ticks ------------------------------------------------------------

def test_ltp_ticks_use_neutral_ids_and_skip_unknown_tokens():
    (t,) = list(groww.ltp_ticks(LTP, IDS, recv_ms=7))
    assert (t.instrument, t.ts_ms, t.ltp, t.recv_ms) == (CE, 1746174479582, 26111.10, 7)
    assert (t.volume, t.oi) == (1250.0, 98765.0)


def test_zero_volume_and_oi_mean_not_sent():
    snap = {"ltp": {"NSE": {"FNO": {"35241": {"tsInMillis": 1.0, "ltp": 87.3, "volume": 0.0, "openInterest": 0.0}}}}}
    (t,) = list(groww.ltp_ticks(snap, IDS, recv_ms=7))
    assert (t.volume, t.oi) == (None, None)


def test_index_ticks():
    ticks = {t.instrument: t.ltp for t in groww.index_ticks(INDEX, IDS, 1)}
    assert ticks == {NIFTY: 24386.7, SENSEX: 73386.7}


def test_depth_ticks_order_levels_and_drop_empty_ones():
    (t,) = list(groww.depth_ticks(DEPTH, IDS, 1))
    assert t.ts_ms == 1746156600000          # seconds normalised to ms
    assert t.bids == (Level(1420.2, 120.0), Level(1420.1, 30.0))
    assert (t.bid, t.bid_qty, t.ask, t.ask_qty) == (1420.2, 120.0, 1420.4, 5.0)
    assert t.ltp is None


def test_empty_snapshots():
    assert list(groww.ltp_ticks(None, IDS, 1)) == []
    assert list(groww.index_ticks({}, IDS, 1)) == []
    assert list(groww.depth_ticks({"NSE": {"FNO": {"35241": None}}}, IDS, 1)) == []


# --- adapter -------------------------------------------------------------------------

class FakeFeed:
    """Stands in for GrowwFeed: records subscriptions, serves set snapshots."""
    made = []

    def __init__(self, api):
        self.subs = {"index": [], "ltp": [], "depth": []}
        self.ltp, self.index, self.depth = None, None, None
        FakeFeed.made.append(self)

    def subscribe_index_value(self, items): self.subs["index"] += items
    def subscribe_ltp(self, items): self.subs["ltp"] += items
    def subscribe_market_depth(self, items): self.subs["depth"] += items

    def _get(self, snap):
        if snap is None:
            raise KeyError("ltp")      # what the SDK does when nothing of this type is subscribed
        return snap

    def get_ltp(self): return self._get(self.ltp)
    def get_index_value(self): return self._get(self.index)
    def get_market_depth(self): return self._get(self.depth)


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


@pytest.fixture
def md():
    FakeFeed.made = []
    clock = Clock()
    m = GrowwMarketData(api=None, contracts=CONTRACTS, feed_factory=FakeFeed, clock=clock, stale_seconds=30)
    m.clock = clock
    m.seen = []
    m.on_tick(m.seen.append)
    return m


def test_satisfies_market_data_interface(md):
    assert isinstance(md, MarketData)
    assert md.instruments() == [Contract(CE, 75)]


def test_connects_and_sends_groww_keys(md):
    md.subscribe([NIFTY, CE])
    md.poll_once()
    (feed,) = FakeFeed.made
    assert feed.subs["index"] == [{"exchange": "NSE", "segment": "CASH", "exchange_token": "NIFTY"}]
    assert feed.subs["ltp"] == feed.subs["depth"] == [{"exchange": "NSE", "segment": "FNO", "exchange_token": "35241"}]


def test_subscribing_later_goes_straight_to_the_live_feed(md):
    md.subscribe([NIFTY])
    md.poll_once()
    md.subscribe([CE, NIFTY])                # NIFTY again is a no-op
    (feed,) = FakeFeed.made
    assert len(feed.subs["index"]) == 1 and len(feed.subs["ltp"]) == 1


def test_unknown_instrument_is_rejected(md):
    with pytest.raises(KeyError):
        md.subscribe([InstrumentId.index("NSE", "BANKNIFTY")])


def test_publishes_each_update_once(md):
    md.subscribe([NIFTY, CE])
    md.poll_once()
    feed = FakeFeed.made[0]
    feed.ltp, feed.index, feed.depth = LTP, INDEX, DEPTH
    assert md.poll_once() == 4               # CE ltp, NIFTY + SENSEX values, CE depth
    assert md.poll_once() == 0               # same snapshot again: nothing new
    feed.index = {"NSE": {"CASH": {"NIFTY": {"tsInMillis": 1746174583295.0, "value": 24390.0}}}}
    md.poll_once()
    assert [t.ltp for t in md.seen if t.instrument == NIFTY] == [24386.7, 24390.0]


def test_reconnects_when_quiet_and_backs_off(md):
    md.subscribe([NIFTY])
    md.poll_once()
    md.clock.t += 31                         # quiet past stale_seconds
    md.poll_once()
    assert len(FakeFeed.made) == 2
    assert FakeFeed.made[1].subs["index"] == FakeFeed.made[0].subs["index"]   # resubscribed
    md.clock.t += 31                         # still quiet, but next attempt waits 60s
    md.poll_once()
    assert len(FakeFeed.made) == 2
    md.clock.t += 30
    md.poll_once()
    assert len(FakeFeed.made) == 3


def test_data_resets_the_quiet_timer(md):
    md.subscribe([NIFTY])
    md.poll_once()
    md.clock.t += 20
    FakeFeed.made[0].index = INDEX
    md.poll_once()
    md.clock.t += 20                         # 40s since connect, but only 20s since data
    md.poll_once()
    assert len(FakeFeed.made) == 1


def test_failed_connect_is_retried(md):
    calls = []

    def flaky(api):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("network down")
        return FakeFeed(api)

    md._feed_factory = flaky
    md.subscribe([NIFTY])
    md.poll_once()                           # fails, retry in 30s
    md.poll_once()
    assert len(calls) == 1
    md.clock.t += 30
    md.poll_once()
    assert len(calls) == 2 and FakeFeed.made[-1].subs["index"]


def test_depth_can_be_turned_off():
    FakeFeed.made = []
    m = GrowwMarketData(api=None, contracts=CONTRACTS, feed_factory=FakeFeed, depth=False)
    m.subscribe([CE])
    m.poll_once()
    assert FakeFeed.made[0].subs["depth"] == []


def test_book_json_round_trip_via_writer(tmp_path):
    from tradeloop.store import Store, TickWriter
    s = Store(tmp_path / "t.db")
    w = TickWriter(s)
    for t in groww.depth_ticks(DEPTH, IDS, 1):
        w.on_tick(t)
    w.flush()
    (book,) = s.conn.execute("SELECT book_json FROM depth").fetchone()
    assert json.loads(book) == {"buy": [[1420.2, 120.0], [1420.1, 30.0]],
                                "sell": [[1420.4, 5.0], [1420.5, 450.0]]}


def test_ten_second_timer_ignores_a_short_pause_and_rebuilds_after_a_long_one():
    FakeFeed.made = []
    clock = Clock()
    m = GrowwMarketData(api=None, contracts=CONTRACTS, feed_factory=FakeFeed, clock=clock, stale_seconds=10)
    m.subscribe([NIFTY])
    m.poll_once()
    clock.t += 9                             # normal silence on the real feed is under 3 s
    m.poll_once()
    assert len(FakeFeed.made) == 1
    clock.t += 2                             # 11 s since the last data
    m.poll_once()
    assert len(FakeFeed.made) == 2
    clock.t += 11                            # still silent, but this attempt waits 20 s (the wait doubles)
    m.poll_once()
    assert len(FakeFeed.made) == 2
    clock.t += 10
    m.poll_once()
    assert len(FakeFeed.made) == 3
