"""Groww adapter (design rule 1): MarketData now, Execution later.

Maps Groww's instrument CSV and feed tokens to neutral IDs, so nothing past this
module sees a Groww field name.

The feed is read as snapshots on our own loop, not via SDK callbacks: callbacks
fire on the SDK's network thread, and the bus is single-threaded. Snapshot shapes
(from the SDK's protobuf parser):
  get_ltp()          -> {exchange: {segment: {token: {"tsInMillis", "ltp", ...}}}}
  get_index_value()  -> {exchange: {segment: {token: {"tsInMillis", "value"}}}}
  get_market_depth() -> {exchange: {segment: {token: {"tsInMillis", "buyBook", "sellBook"}}}}

Reconnect: the SDK's NATS client retries for about two minutes, then gives up for
good. If no new data arrives for `stale_seconds`, we build a fresh feed (new socket
token) and resubscribe everything, backing off up to MAX_BACKOFF between attempts.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

import pandas as pd
from growwapi import GrowwAPI, GrowwFeed

from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.types import Contract, Level, Tick

log = logging.getLogger("groww")

INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
MAX_BACKOFF = 300.0

Key = tuple[str, str, str]   # (exchange, segment, exchange_token): how Groww names a feed

# Index feed keys, per the Groww feed docs (indices aren't option rows in the CSV).
INDEX_KEYS: dict[InstrumentId, Key] = {
    InstrumentId.index("NSE", "NIFTY"): ("NSE", "CASH", "NIFTY"),
    InstrumentId.index("BSE", "SENSEX"): ("BSE", "CASH", "1"),
}


# --- instruments -------------------------------------------------------------

def load_contracts(source: str = INSTRUMENT_CSV_URL) -> dict[Key, Contract]:
    df = pd.read_csv(source, dtype={"exchange_token": str}, low_memory=False)
    return contracts_from_rows(df)


def contracts_from_rows(df: pd.DataFrame) -> dict[Key, Contract]:
    """Option rows of the instrument CSV -> {feed key: Contract}.

    Other instrument types are skipped until a track needs them. Malformed rows
    are skipped and counted, so one bad row can't stop the day's recording.
    """
    opts = df[(df["segment"] == "FNO") & df["instrument_type"].isin(["CE", "PE"])].copy()
    opts["expiry"] = pd.to_datetime(opts["expiry_date"], errors="coerce").dt.date
    out, bad = {}, 0
    for r in opts.itertuples(index=False):
        if pd.isna(r.expiry):              # NaT would slip past InstrumentId's None check
            bad += 1
            continue
        try:
            iid = InstrumentId.option(r.exchange, r.underlying_symbol, r.expiry,
                                      float(r.strike_price), r.instrument_type)
            out[(r.exchange, "FNO", str(r.exchange_token))] = Contract(iid, int(r.lot_size))
        except (TypeError, ValueError):
            bad += 1
    if bad:
        log.warning("Skipped %d malformed option rows in the instrument CSV", bad)
    return out


# --- snapshots -> ticks --------------------------------------------------------

def _ms(ts) -> int:
    ts = float(ts)
    return int(ts * 1000) if ts < 1e12 else int(ts)  # some payloads carry seconds


def _num(v) -> float | None:
    return None if v is None else float(v)


def _walk(root):
    for exchange, segments in (root or {}).items():
        if not isinstance(segments, dict):
            continue
        for segment, tokens in segments.items():
            if not isinstance(tokens, dict):
                continue
            for token, data in tokens.items():
                if isinstance(data, dict) and "tsInMillis" in data:
                    yield (exchange, segment, str(token)), data


def ltp_ticks(snapshot, ids: dict[Key, InstrumentId], recv_ms: int):
    root = (snapshot or {}).get("ltp", snapshot)   # SDK docs show it wrapped; get_ltp() isn't
    for key, d in _walk(root):
        iid = ids.get(key)
        if iid is not None and d.get("ltp") is not None:
            yield Tick(iid, _ms(d["tsInMillis"]), ltp=float(d["ltp"]), recv_ms=recv_ms,
                       volume=_num(d.get("volume")), oi=_num(d.get("openInterest")))


def index_ticks(snapshot, ids: dict[Key, InstrumentId], recv_ms: int):
    for key, d in _walk(snapshot):
        iid = ids.get(key)
        if iid is not None and d.get("value") is not None:
            yield Tick(iid, _ms(d["tsInMillis"]), ltp=float(d["value"]), recv_ms=recv_ms)


def _levels(book) -> tuple[Level, ...]:
    """Groww books are {"1": {"price", "qty"}, ...}; empty levels come as price 0."""
    levels = (book[k] for k in sorted(book or {}, key=int))
    return tuple(Level(float(lv["price"]), float(lv["qty"]))
                 for lv in levels if float(lv.get("price") or 0) > 0)


def depth_ticks(snapshot, ids: dict[Key, InstrumentId], recv_ms: int):
    for key, d in _walk(snapshot):
        iid = ids.get(key)
        if iid is None:
            continue
        bids, asks = _levels(d.get("buyBook")), _levels(d.get("sellBook"))
        yield Tick(iid, _ms(d["tsInMillis"]), recv_ms=recv_ms, bids=bids, asks=asks,
                   bid=bids[0].price if bids else None, bid_qty=bids[0].qty if bids else None,
                   ask=asks[0].price if asks else None, ask_qty=asks[0].qty if asks else None)


# --- MarketData ------------------------------------------------------------------

class GrowwMarketData:
    """Streams neutral Ticks from Groww's live feed. Implements core MarketData."""

    def __init__(self, api: GrowwAPI, contracts: dict[Key, Contract] | None = None, *,
                 depth: bool = True, poll_seconds: float = 0.5, stale_seconds: float = 30.0,
                 feed_factory: Callable[[GrowwAPI], GrowwFeed] = GrowwFeed,
                 clock: Callable[[], float] = time.monotonic):
        self._api = api
        self._contracts = load_contracts() if contracts is None else contracts
        self._ids: dict[Key, InstrumentId] = {k: c.instrument for k, c in self._contracts.items()}
        self._ids.update({k: i for i, k in INDEX_KEYS.items()})
        self._keys: dict[InstrumentId, Key] = {i: k for k, i in self._ids.items()}
        self._depth, self._poll, self._stale = depth, poll_seconds, stale_seconds
        self._feed_factory, self._clock = feed_factory, clock
        self._feed: GrowwFeed | None = None
        self._wanted: set[InstrumentId] = set()
        self._handlers: list[Callable[[Tick], None]] = []
        self._last_ts: dict[tuple[InstrumentId, str], int] = {}
        self._last_data = clock()
        self._backoff = stale_seconds
        self._next_connect = 0.0
        self._stopped = False

    def instruments(self) -> list[Contract]:
        return list(self._contracts.values())

    def subscribe(self, ids: Iterable[InstrumentId]) -> None:
        new = sorted({i for i in ids if i not in self._wanted}, key=str)
        unknown = [str(i) for i in new if i not in self._keys]
        if unknown:
            raise KeyError(f"not in Groww's instrument list: {unknown[:5]}")
        self._wanted.update(new)
        if self._feed is not None and new:
            self._send_subscriptions(self._feed, new)

    def on_tick(self, handler: Callable[[Tick], None]) -> None:
        self._handlers.append(handler)

    def run(self) -> None:
        while not self._stopped:
            try:
                self.poll_once()
            except Exception:
                log.exception("Feed poll failed")
            time.sleep(self._poll)
        if self._feed is not None:
            _close_feed(self._feed)

    def stop(self) -> None:
        self._stopped = True

    def poll_once(self) -> int:
        """Publish new updates; (re)connect if there's no feed or it went quiet. Returns ticks published."""
        now = self._clock()
        n = 0
        if self._feed is not None:
            n = self._publish(self._feed)
            if n:
                self._last_data, self._backoff = now, self._stale
        quiet = self._wanted and now - self._last_data > self._stale
        if (self._feed is None or quiet) and now >= self._next_connect:
            self._connect(now)
        return n

    # --- internals -------------------------------------------------------------

    def _connect(self, now: float) -> None:
        if self._feed is not None:
            log.warning("No feed data for %.0fs; reconnecting", now - self._last_data)
            _close_feed(self._feed)
            self._feed = None
        try:
            feed = self._feed_factory(self._api)
            self._send_subscriptions(feed, sorted(self._wanted, key=str))
            self._feed, self._last_data = feed, now
            log.info("Feed connected, %d instruments subscribed", len(self._wanted))
        except Exception:
            log.exception("Feed connect failed; retrying in %.0fs", self._backoff)
        self._next_connect = now + self._backoff
        self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    def _send_subscriptions(self, feed: GrowwFeed, ids: list[InstrumentId]) -> None:
        indices, options = [], []
        for i in ids:
            exchange, segment, token = self._keys[i]
            item = {"exchange": exchange, "segment": segment, "exchange_token": token}
            (indices if i.kind is Kind.IDX else options).append(item)
        if indices:
            feed.subscribe_index_value(indices)
        if options:
            feed.subscribe_ltp(options)
            if self._depth:
                feed.subscribe_market_depth(options)

    def _publish(self, feed: GrowwFeed) -> int:
        recv = int(time.time() * 1000)
        sources = [("ltp", feed.get_ltp, ltp_ticks), ("ltp", feed.get_index_value, index_ticks)]
        if self._depth:
            sources.append(("depth", feed.get_market_depth, depth_ticks))
        new = []
        for kind, get, convert in sources:
            try:
                snapshot = get()
            except KeyError:          # the SDK raises this when nothing of this type is subscribed
                continue
            for t in convert(snapshot, self._ids, recv):
                key = (t.instrument, kind)
                if t.ts_ms > self._last_ts.get(key, -1):   # snapshots repeat until the next update
                    self._last_ts[key] = t.ts_ms
                    new.append(t)
        for t in new:
            for handler in self._handlers:
                handler(t)
        return len(new)


def _close_feed(feed) -> None:
    """Best effort: close the NATS connection and drop it from the SDK's class-level
    client cache. The SDK has no public close, so this reaches into its internals."""
    try:
        GrowwFeed._nats_clients.pop(feed._client_key, None)
        client = feed._nats_client
        asyncio.run_coroutine_threadsafe(client._socket.close(), client._loop)
    except Exception:
        log.debug("Could not close old feed", exc_info=True)
