"""Payloads copied from the Groww Python SDK Feed docs."""
import json

from tradeloop import feed_parse

LTP = {"ltp": {"NSE": {"CASH": {"2885": {"tsInMillis": 1746174479582.0, "ltp": 1419.1}},
                       "FNO": {"35241": {"tsInMillis": 1746174479582.0, "ltp": 26111.10}}}}}
INDEX = {"NSE": {"CASH": {"NIFTY": {"tsInMillis": 1746174582295.0, "value": 24386.7}}},
         "BSE": {"CASH": {"1": {"tsInMillis": 1746174582295.0, "value": 73386.7}}}}
DEPTH = {"NSE": {"FNO": {"35241": {
    "tsInMillis": 1746156600.0,
    "buyBook": {"2": {"price": 1420.1, "qty": 30.0}, "1": {"price": 1420.2, "qty": 120.0}},
    "sellBook": {"1": {"price": 1420.4, "qty": 5.0}, "2": {"price": 1420.5, "qty": 450.0}}}}}}


def test_ltp_rows():
    rows = list(feed_parse.ltp_rows(LTP, recv_ms=1))
    assert (1746174479582, 1, "NSE", "FNO", "35241", 26111.10) in rows
    assert len(rows) == 2


def test_index_rows():
    rows = sorted(feed_parse.index_rows(INDEX, recv_ms=1))
    assert [r[3] for r in rows] == ["1", "NIFTY"]
    assert rows[1][4] == 24386.7


def test_depth_rows_best_level_and_seconds_timestamp():
    (row,) = list(feed_parse.depth_rows(DEPTH, recv_ms=1))
    ts, _, ex, seg, tok, bid_px, bid_qty, ask_px, ask_qty, book = row
    assert ts == 1746156600000          # seconds normalised to ms
    assert (bid_px, bid_qty, ask_px, ask_qty) == (1420.2, 120.0, 1420.4, 5.0)
    assert json.loads(book)["sell"]["2"]["qty"] == 450.0


def test_empty_snapshots():
    assert list(feed_parse.ltp_rows(None, 1)) == []
    assert list(feed_parse.index_rows({}, 1)) == []
    assert list(feed_parse.depth_rows({"NSE": {}}, 1)) == []
