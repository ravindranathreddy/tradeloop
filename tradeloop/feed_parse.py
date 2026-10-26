"""Turn Groww feed snapshots into flat rows. Pure functions, so they're easy to test.

Snapshot shapes follow the Groww Python SDK docs (Feed page):
  get_ltp()          -> {"ltp": {exchange: {segment: {token: {"tsInMillis", "ltp"}}}}}
  get_index_value()  -> {exchange: {segment: {token: {"tsInMillis", "value"}}}}
  get_market_depth() -> {exchange: {segment: {token: {"tsInMillis", "buyBook", "sellBook"}}}}
"""
import json


def _ms(ts) -> int:
    ts = float(ts)
    return int(ts * 1000) if ts < 1e12 else int(ts)  # some payloads carry seconds


def _walk(root):
    for exchange, segments in (root or {}).items():
        if not isinstance(segments, dict):
            continue
        for segment, tokens in segments.items():
            if not isinstance(tokens, dict):
                continue
            for token, data in tokens.items():
                if isinstance(data, dict) and "tsInMillis" in data:
                    yield exchange, segment, str(token), data


def ltp_rows(snapshot, recv_ms: int):
    root = (snapshot or {}).get("ltp", snapshot)
    for exchange, segment, token, d in _walk(root):
        if d.get("ltp") is not None:
            yield (_ms(d["tsInMillis"]), recv_ms, exchange, segment, token, float(d["ltp"]))


def index_rows(snapshot, recv_ms: int):
    for exchange, _segment, token, d in _walk(snapshot):
        if d.get("value") is not None:
            yield (_ms(d["tsInMillis"]), recv_ms, exchange, token, float(d["value"]))


def _best(book):
    if not book:
        return None, None
    level = book.get("1") or book.get(1) or book[min(book, key=lambda k: int(k))]
    return float(level["price"]), float(level["qty"])


def depth_rows(snapshot, recv_ms: int):
    for exchange, segment, token, d in _walk(snapshot):
        bid_px, bid_qty = _best(d.get("buyBook"))
        ask_px, ask_qty = _best(d.get("sellBook"))
        book = {"buy": d.get("buyBook"), "sell": d.get("sellBook")}
        yield (_ms(d["tsInMillis"]), recv_ms, exchange, segment, token,
               bid_px, bid_qty, ask_px, ask_qty, json.dumps(book, separators=(",", ":")))
