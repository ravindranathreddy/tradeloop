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
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
from growwapi import GrowwAPI, GrowwFeed

from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.types import Contract, Level, Tick

log = logging.getLogger("groww")
IST = ZoneInfo("Asia/Kolkata")

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


def _present(v) -> float | None:
    """The SDK prints every proto field, so a field Groww didn't send arrives as 0. The live
    LTP stream rarely fills volume or OI (seen 27 Oct 2026: volume never, OI on <1% of
    updates), so 0 means unknown here, not zero. Volume and OI come from candles instead."""
    return None if v is None or float(v) == 0 else float(v)


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
                       volume=_present(d.get("volume")), oi=_present(d.get("openInterest")))


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


# --- history (Groww's backtesting API) ------------------------------------------------

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# groww_symbol for index candles in the backtesting API.
HISTORY_INDEX_SYMBOLS: dict[InstrumentId, str] = {
    InstrumentId.index("NSE", "NIFTY"): "NSE-NIFTY",
    InstrumentId.index("BSE", "SENSEX"): "BSE-SENSEX",
}


def _groww_date(s: str) -> date:
    return date(2000 + int(s[5:7]), MONTHS.index(s[2:5]) + 1, int(s[:2]))


def derivative_from_groww_symbol(symbol: str) -> InstrumentId:
    """'NSE-NIFTY-26Jun25-25000-CE' -> NSE|NIFTY|2025-06-26|25000|CE;
    'NSE-NIFTY-26Jun25-FUT' -> NSE|NIFTY|2025-06-26|FUT."""
    parts = symbol.split("-")
    if len(parts) == 4 and parts[3] == "FUT":
        exchange, underlying, expiry, _ = parts
        return InstrumentId(exchange, underlying, Kind.FUT, _groww_date(expiry))
    exchange, underlying, expiry, strike, kind = parts
    return InstrumentId.option(exchange, underlying, _groww_date(expiry), float(strike), kind)


def groww_symbol(iid: InstrumentId) -> str:
    if iid.kind is Kind.IDX:
        return HISTORY_INDEX_SYMBOLS[iid]
    e = iid.expiry
    if iid.kind is Kind.FUT:
        return f"{iid.exchange}-{iid.underlying}-{e.day:02d}{MONTHS[e.month - 1]}{e.year % 100:02d}-FUT"
    strike = int(iid.strike) if float(iid.strike).is_integer() else iid.strike
    return f"{iid.exchange}-{iid.underlying}-{e.day:02d}{MONTHS[e.month - 1]}{e.year % 100:02d}-{strike}-{iid.kind.value}"


def candle_rows(iid: InstrumentId, candles) -> list[tuple]:
    """Groww candles [ts, o, h, l, c, volume, oi] (ts in IST) -> rows keyed by neutral ID.
    Volume and OI stay None where Groww has none (indices; OI between exchange updates)."""
    rows = []
    for ts, o, h, l, c, *rest in candles or []:      # daily index candles can omit volume/OI
        vol, oi = (rest + [None, None])[:2]
        t = datetime.fromisoformat(ts).replace(tzinfo=IST)
        rows.append((str(iid), int(t.timestamp() * 1000), o, h, l, c, vol, oi))
    return rows


class GrowwHistory:
    """Groww's backtesting API with neutral IDs in and out, throttled and retried.

    Groww doesn't document a limit for these calls; ~3.6 calls/s hit one, so we
    space calls `min_interval` apart. Rate limits and network blips (resets,
    timeouts) are retried with backoff; anything else is raised at once. The SDK
    has no timeout by default, so a hung connection would stall forever: we pass one.
    """

    def __init__(self, api: GrowwAPI, *, min_interval: float = 0.5, retries: int = 6, timeout: float = 30,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self._api, self._min_interval, self._retries, self._timeout = api, min_interval, retries, timeout
        self._sleep, self._clock = sleep, clock
        self._last_call = float("-inf")
        self.calls = 0

    def expiries(self, index: InstrumentId, year: int, month: int | None = None) -> list[date]:
        r = self._call(self._api.get_expiries, index.exchange, index.underlying, year=year, month=month)
        return [date.fromisoformat(d) for d in r.get("expiries") or []]

    def contracts(self, index: InstrumentId, expiry: date) -> list[InstrumentId]:
        r = self._call(self._api.get_contracts, index.exchange, index.underlying, expiry.isoformat())
        out = []
        for s in r.get("contracts") or []:
            try:
                out.append(derivative_from_groww_symbol(s))
            except (ValueError, IndexError):
                log.warning("Skipping unrecognised contract symbol %s", s)
        return out

    def candles(self, iid: InstrumentId, start: datetime, end: datetime, interval: str = "1minute") -> list[tuple]:
        """interval: "1minute" or "1day". An index's daily close is the official close,
        which expiring options settle on (not the last traded value)."""
        segment = "CASH" if iid.kind is Kind.IDX else "FNO"
        r = self._call(self._api.get_historical_candles, iid.exchange, segment, groww_symbol(iid),
                       start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), interval)
        return candle_rows(iid, r.get("candles"))

    def _call(self, fn, *args, **kw):
        delay = 2.0
        for attempt in range(self._retries + 1):
            wait = self._last_call + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last_call = self._clock()
            self.calls += 1
            try:
                return fn(*args, timeout=self._timeout, **kw)
            except Exception as e:
                if not _transient(e) or attempt == self._retries:
                    raise
                log.info("%s; retrying in %.0fs", str(e)[:80], delay)
                self._sleep(delay)
                delay = min(delay * 2, 60.0)


def _transient(e: Exception) -> bool:
    """Worth retrying: rate limits and network blips, not bad requests or auth errors."""
    import requests                       # the SDK's HTTP client
    if isinstance(e, (ConnectionError, TimeoutError, requests.ConnectionError, requests.Timeout)):
        return True
    return "rate limit" in str(e).lower()
