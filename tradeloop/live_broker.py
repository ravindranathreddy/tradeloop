"""The live broker: GrowwExecution behind the same surface the paper trader already uses.

PaperTrader drives a broker through on_tick / step / fresh / working / load_positions plus the
Execution methods. LiveBroker supplies those on top of GrowwExecution, so the strategy, risk gate,
kill switch, watchdog and ledger run unchanged and only the broker underneath differs.

Two things differ from paper, both because Groww answers on its own clock:
  fills       arrive by asking Groww. While an order is working it is polled about once a second;
              a fill is booked on the first tick (or watchdog round) after the poll sees it.
  prices      a MARKET order is sent as a marketable LIMIT: best ask plus a few ticks to buy, best
              bid minus a few to sell, from the last order book. It fills like a market order but
              can't run through a thin book. With no book at all the order goes out as MARKET.
              A book older than `stale_ms` still prices the order (the kill switch must be able to
              exit when the feed is down) but with a wide buffer.

Threads (threaded=True, how the recorder runs it): every blocking Groww call (place, cancel, poll,
cancel-all) runs on ONE worker thread, so a slow or hung Groww never stalls the feed handler or the
watchdog; the worst case is the call's own timeout, and queued orders wait behind it, in order (a
kill's cancel-all always runs before the exits queued after it). The worker never touches the
ledger. It puts what Groww said on an event queue, and `drain()` hands those events to the handlers
on the caller's thread (on_tick / step / tend, all under the trader's lock). The broker's own
`working()` view changes inside drain(), in the same step as the ledger, so the kill switch never
sees an order gone from `working()` while its fill is still missing from the ledger (that gap would
send a second exit and flip the position).

threaded=False runs each command inline: the unit tests and the standalone flatten use it.

Nothing here arms the adapter: `armed` stays whatever GrowwExecution was built with.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from tradeloop.brokers.groww_execution import GrowwExecution
from tradeloop.brokers.groww_orders import Symbol
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Order, OrderStatus, OrderType, OrderUpdate, Position, Side, Tick

log = logging.getLogger("live")
MINUTE_MS = 60_000
CANCEL_ALL_TRIES = 3


@dataclass(slots=True)
class _Book:
    recv_ms: int
    bid: float | None
    ask: float | None


class LiveBroker:
    def __init__(self, execution: GrowwExecution, symbols: dict[InstrumentId, Symbol], *, stale_ms: int = 10_000,
                 max_wait_minutes: float = 2, buffer_ticks: int = 4, stale_buffer_pct: float = 10,
                 poll_ms: int = 1000, threaded: bool = False,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], None] = time.sleep):
        self.execution, self._symbols = execution, symbols
        self._stale_ms, self._wait_ms = stale_ms, int(max_wait_minutes * MINUTE_MS)
        self._buffer_ticks, self._stale_pct, self._poll_ms = buffer_ticks, stale_buffer_pct, poll_ms
        self._clock, self._sleep = clock_ms, sleep
        self._books: dict[InstrumentId, _Book] = {}
        self._placed_ms: dict[str, int] = {}
        self._cancel_asked_ms: dict[str, int] = {}
        self._last_poll_ms = 0
        self._working: dict[str, Order] = {}                  # what the trader sees; changes only in drain()
        self._seen: set[str] = set()
        self._fill_handlers: list[Callable[[Fill], None]] = []
        self._update_handlers: list[Callable[[OrderUpdate], None]] = []
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._commands: queue.Queue = queue.Queue()
        self._poll_queued = False
        self._thread: threading.Thread | None = None
        execution.on_fill(lambda f: self._events.put(("fill", f)))
        execution.on_order_update(lambda u: self._events.put(("update", u)))
        if threaded:
            self._thread = threading.Thread(target=self._loop, name="groww-worker", daemon=True)
            self._thread.start()

    @property
    def armed(self) -> bool:
        return self.execution.armed

    # --- market data and the clock -------------------------------------------------------

    def on_tick(self, tick: Tick) -> None:
        if tick.bids or tick.asks:
            self._books[tick.instrument] = _Book(tick.recv_ms or self._clock(),
                                                 tick.bids[0].price if tick.bids else None,
                                                 tick.asks[0].price if tick.asks else None)
        self._pump()

    def reference_price(self, instrument: InstrumentId) -> float | None:
        """Mid of the last book seen, any age (a reference for margin quotes, not for orders)."""
        b = self._books.get(instrument)
        prices = [p for p in (b.bid, b.ask) if p] if b else []
        return sum(prices) / len(prices) if prices else None

    def fresh(self, instrument: InstrumentId) -> bool:
        book = self._books.get(instrument)
        return book is not None and self._clock() - book.recv_ms <= self._stale_ms

    def step(self) -> None:
        """About once a minute: poll now, and cancel what has waited too long (the price ran away)."""
        now = self._clock()
        if self._thread is None:
            self._poll_inline(now, force=True)
        elif not self._poll_queued:
            self._poll_queued = True
            self._commands.put(("poll",))
        self._cancel_overdue(now)
        self.drain()

    def tend(self) -> None:
        """Between ticks (watchdog thread): keep events and cancels moving when the feed is quiet."""
        self._pump()
        self._cancel_overdue(self._clock())

    def seed_prices(self, prices: dict[InstrumentId, float]) -> None:
        """Last known prices for a process that has no feed (the standalone kill). Treated as stale, so
        orders get the wide buffer."""
        for i, p in prices.items():
            self._books[i] = _Book(0, p, p)

    def _pump(self) -> None:
        if self._thread is None and self.execution.working():
            self._poll_inline(self._clock())
        self.drain()

    def _poll_inline(self, now: int, force: bool = False) -> None:
        if force or now - self._last_poll_ms >= self._poll_ms:
            self._last_poll_ms = now
            self.execution.poll()

    def _cancel_overdue(self, now: int) -> None:
        for o in list(self._working.values()):
            asked = self._cancel_asked_ms.get(o.client_ref)
            if now - self._placed_ms.get(o.client_ref, now) >= self._wait_ms and (asked is None or now - asked > 30_000):
                self._cancel_asked_ms[o.client_ref] = now
                log.warning("%s %d %s unfilled after %ds: cancelling", o.side.value, o.qty, o.instrument,
                            self._wait_ms // 1000)
                self.cancel(o.client_ref)

    # --- Execution -----------------------------------------------------------------------

    def place(self, order: Order) -> None:
        if order.client_ref in self._seen:                    # idempotent on client_ref, here as at Groww
            return
        self._seen.add(order.client_ref)
        self._placed_ms.setdefault(order.client_ref, self._clock())
        self._working[order.client_ref] = order
        self._submit(("place", self._priced(order), order.client_ref))

    def cancel(self, client_ref: str) -> None:
        self._submit(("cancel", client_ref))

    def cancel_all(self) -> None:
        self._submit(("cancel_all",))

    def positions(self) -> list[Position]:
        return self.execution.positions()

    def load_positions(self, positions: dict[InstrumentId, int]) -> None:
        """Nothing to load: Groww holds the positions. Startup reconciliation compares them with the ledger."""

    def on_order_update(self, handler: Callable[[OrderUpdate], None]) -> None:
        self._update_handlers.append(handler)

    def on_fill(self, handler: Callable[[Fill], None]) -> None:
        self._fill_handlers.append(handler)

    def working(self) -> list[Order]:
        return list(self._working.values())

    def drain(self) -> None:
        """Hand what Groww said to the handlers, in order, on this thread (hold the trader's lock).
        Fills come before the status update that follows them, and an order leaves `working()` only
        here, together with its fill reaching the ledger."""
        while True:
            try:
                kind, ev = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "fill":
                    for h in self._fill_handlers:
                        h(ev)
                else:
                    if ev.status.is_final:
                        self._working.pop(ev.client_ref, None)
                    for h in self._update_handlers:
                        h(ev)
            except Exception:
                log.exception("ALERT handling %s %s failed", kind, getattr(ev, "client_ref", ev))

    def join(self, timeout: float = 5) -> bool:
        """Wait for the worker to finish what is queued (tests, shutdown). True if it did."""
        if self._thread is None:
            return True
        done = threading.Event()
        self._commands.put(("done", done))
        return done.wait(timeout)

    def close(self, timeout: float = 5) -> None:
        """Finish what is queued, then stop the worker; hand over the last events."""
        if self._thread is not None:
            self.join(timeout)
            self._commands.put(("stop",))
            self._thread.join(timeout)
        self.drain()

    # --- the worker ------------------------------------------------------------------------

    def _submit(self, cmd: tuple) -> None:
        if self._thread is not None:
            self._commands.put(cmd)
        else:
            self._run(cmd)
            self.drain()

    def _loop(self) -> None:
        while True:
            try:
                cmd = self._commands.get(timeout=0.05)
            except queue.Empty:
                cmd = None
            if cmd is not None:
                if cmd[0] == "stop":
                    return
                self._run(cmd)
            now = self._clock()
            if now - self._last_poll_ms >= self._poll_ms and self.execution.working():
                self._run(("poll",))

    def _run(self, cmd: tuple) -> None:
        kind = cmd[0]
        try:
            if kind == "place":
                self.execution.place(cmd[1])
            elif kind == "cancel":
                self.execution.cancel(cmd[1])
            elif kind == "cancel_all":
                self._cancel_all()
            elif kind == "poll":
                self._poll_queued = False
                self._last_poll_ms = self._clock()
                self.execution.poll()
            elif kind == "done":
                cmd[1].set()
        except Exception as e:                                 # the worker must outlive any one bad command
            if kind == "cancel_all" and self._thread is None:
                raise                                          # inline: the trader's own retry handles it
            log.exception("ALERT live broker: %s failed", kind)
            if kind == "place":
                self._events.put(("update", OrderUpdate(cmd[2], OrderStatus.REJECTED, self._clock(),
                                                        message=f"internal error: {type(e).__name__}; check the account")))

    def _cancel_all(self) -> None:
        """Inline, a failure raises (the trader retries). On the worker it is retried here, before any
        later command: a retry queued behind the kill's exit orders would cancel them."""
        if self._thread is None:
            return self.execution.cancel_all()
        for attempt in range(1, CANCEL_ALL_TRIES + 1):
            try:
                return self.execution.cancel_all()
            except Exception as e:
                log.error("ALERT cancel-all failed (try %d of %d): %s: %s", attempt, CANCEL_ALL_TRIES, type(e).__name__, e)
                if attempt < CANCEL_ALL_TRIES:
                    self._sleep(1)

    # --- pricing -----------------------------------------------------------------------------

    def _priced(self, order: Order) -> Order:
        sym, book = self._symbols.get(order.instrument), self._books.get(order.instrument)
        if order.order_type is not OrderType.MARKET or sym is None:
            return order
        ref = None if book is None else (book.ask if order.side is Side.BUY else book.bid)
        if not ref:
            log.warning("no order book for %s: sending %s as MARKET", order.instrument, order.client_ref)
            return order
        stale = self._clock() - book.recv_ms > self._stale_ms
        tick = sym.tick_size
        buffer = max(ref * self._stale_pct / 100, 10 * tick) if stale else self._buffer_ticks * tick
        if order.side is Side.BUY:
            price = math.ceil(round((ref + buffer) / tick, 6)) * tick
        else:
            price = max(math.floor(round((ref - buffer) / tick, 6)) * tick, tick)
        price = round(price, 2)
        log.info("%s %d %s: MARKET sent as LIMIT %.2f (%s %.2f%s)", order.side.value, order.qty, order.instrument,
                 price, "ask" if order.side is Side.BUY else "bid", ref, ", STALE book" if stale else "")
        return replace(order, order_type=OrderType.LIMIT, price=price)
