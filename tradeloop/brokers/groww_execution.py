"""Groww `Execution` adapter (stage 2): neutral orders in, neutral updates and fills out.

    api    a logged-in growwapi.GrowwAPI (or anything with the same methods; tests pass a fake)
    armed  False by default: until it is switched on, every order is refused. Real orders are a
           deliberate step, never a side effect of wiring this up.

Rules this adapter keeps (the SDK and Groww's docs leave them to us):

  - Every call carries a timeout; the SDK's default is to wait forever.
  - No blind retry. Before sending, ask Groww whether the order's reference already exists. After
    a failure that leaves the order's fate unknown (timeout, dropped connection, anything
    unrecognised) look it up and, if it isn't there, do NOT send again: keep looking for
    `unverified_grace_s` seconds, then give up with an ALERT. A late-arriving duplicate would
    double a position; a missed order is caught by reconciliation. Only a rate-limit error (nothing
    was accepted) is retried.
  - If Groww can't be asked at all, the order is refused rather than sent unchecked.
  - Fills come from `poll()`: Groww reports cumulative filled quantity and average price, which
    FillTracker turns into one Fill per increase. Call poll() about once a second.
  - Orders of ours carry the 'tl' reference prefix; `untagged_orders()` lists any that don't
    (Rule #1).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from tradeloop.brokers import groww_orders as g
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Fill, Order, OrderStatus, OrderUpdate, Position

log = logging.getLogger("groww.exec")
_WORKING = {"NEW", "ACKED", "APPROVED", "TRIGGER_PENDING", "MODIFICATION_REQUESTED"}
MAX_PAGES = 40


class UnexpectedResponse(ValueError):
    pass


class LookupFailed(RuntimeError):
    """Groww could not be asked whether an order exists."""


@dataclass(slots=True)
class _Tracked:
    order: Order
    reference: str
    groww_id: str | None = None
    status: OrderStatus | None = None
    filled: int = 0
    sent_ms: int = 0
    final: bool = False
    cancel_when_found: bool = False


def _listing(resp, keys: tuple[str, ...]) -> list[dict]:
    """The rows of a list response. The SDK doesn't say which key holds them, so accept the usual
    ones or a single list-valued field, and refuse anything else."""
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        raise UnexpectedResponse(f"expected a list or dict, got {type(resp).__name__}")
    for k in keys:
        if isinstance(resp.get(k), list):
            return resp[k]
    lists = [v for v in resp.values() if isinstance(v, list)]
    if len(lists) == 1:
        return lists[0]
    if not resp:
        return []
    raise UnexpectedResponse(f"cannot find the rows in a response with keys {sorted(resp)}")


class GrowwExecution:
    def __init__(self, api, symbols: dict[InstrumentId, g.Symbol], *, armed: bool = False, timeout: int = 10,
                 unverified_grace_s: float = 30, rate_limit_retries: int = 3,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], None] = time.sleep):
        self.api, self._symbols, self._by_name = api, symbols, g.by_trading_symbol(symbols)
        self.armed, self._timeout, self._grace_ms = armed, timeout, int(unverified_grace_s * 1000)
        self._retries, self._clock, self._sleep = rate_limit_retries, clock_ms, sleep
        self._tracked: dict[str, _Tracked] = {}
        self._fills = g.FillTracker()
        self._fill_handlers: list[Callable[[Fill], None]] = []
        self._update_handlers: list[Callable[[OrderUpdate], None]] = []
        self._lock = threading.RLock()

    # --- Execution ---------------------------------------------------------------------------

    def on_order_update(self, handler: Callable[[OrderUpdate], None]) -> None:
        self._update_handlers.append(handler)

    def on_fill(self, handler: Callable[[Fill], None]) -> None:
        self._fill_handlers.append(handler)

    def working(self) -> list[Order]:
        # No lock: place() holds it through its HTTP call, and asking what is working must never wait on Groww.
        return [t.order for t in list(self._tracked.values()) if not t.final]

    def place(self, order: Order) -> None:
        with self._lock:
            if order.client_ref in self._tracked:
                return                                          # idempotent on client_ref
            sym = self._symbols.get(order.instrument)
            if not self.armed:
                return self._refuse(order, OrderStatus.REJECTED_RISK, "Groww adapter is not armed: no real orders")
            if sym is None:
                return self._refuse(order, OrderStatus.REJECTED, f"{order.instrument} is not a Groww contract")
            try:
                params = g.place_params(order, sym)
            except g.OrderRejected as e:
                return self._refuse(order, OrderStatus.REJECTED_RISK, str(e))
            t = self._tracked[order.client_ref] = _Tracked(order, params["order_reference_id"], sent_ms=self._clock())
            self._emit(OrderUpdate(order.client_ref, OrderStatus.SENT, self._clock()))
            try:
                known = self._lookup(t)
            except LookupFailed as e:
                log.error("ALERT cannot check Groww for %s before sending, order refused: %s", t.reference, e)
                return self._finish(t, OrderStatus.REJECTED, "could not verify with Groww before sending; not sent")
            if known is not None:
                log.warning("Groww already has %s: adopting it, not sending again", t.reference)
                return self._apply(t, known)
            self._send(t, params)

    def cancel(self, client_ref: str) -> None:
        with self._lock:
            t = self._tracked.get(client_ref)
            if t is None or t.final:
                return
            if t.groww_id is None:
                t.cancel_when_found = True                       # still unverified: cancel once it shows up
                return
            self._cancel_id(t.groww_id)

    def cancel_all(self) -> None:
        """Cancel every working order Groww shows for the account, ours or not (kill switch)."""
        with self._lock:
            for t in self._tracked.values():
                if not t.final and t.groww_id is None:
                    t.cancel_when_found = True
            for row in self._all_orders():
                if str(row.get("order_status", "")).upper() not in _WORKING:
                    continue
                if not g.is_ours(row.get("order_reference_id")):
                    log.error("ALERT cancelling an order that is not ours: %s", row)
                if row.get("groww_order_id"):
                    self._cancel_id(row["groww_order_id"])

    def positions(self) -> list[Position]:
        resp = self.api.get_positions_for_user(segment=g.SEGMENT, timeout=self._timeout)
        return g.positions(_listing(resp, ("positions",)), self._by_name)

    # --- polling -----------------------------------------------------------------------------

    def poll(self) -> None:
        """Bring every working order up to date: emit fills first, then the new status."""
        with self._lock:
            for t in [t for t in self._tracked.values() if not t.final]:
                try:
                    self._poll_one(t)
                except Exception:
                    log.exception("ALERT could not update order %s (%s)", t.order.client_ref, t.reference)

    def _poll_one(self, t: _Tracked) -> None:
        if t.groww_id is None:
            try:
                raw = self._lookup(t)
            except LookupFailed:
                return
            if raw is None:
                if self._clock() - t.sent_ms > self._grace_ms:
                    log.error("ALERT order %s never appeared at Groww; giving up (reconciliation will catch a late one)", t.reference)
                    self._finish(t, OrderStatus.REJECTED, "never appeared at Groww")
                return
            return self._apply(t, raw)
        try:
            raw = self.api.get_order_detail(segment=g.SEGMENT, groww_order_id=t.groww_id, timeout=self._timeout)
        except Exception as e:
            if g.is_not_found(e):
                log.error("ALERT Groww has no order %s that we sent (%s)", t.groww_id, t.reference)
            return                                               # try again next poll
        self._apply(t, raw)

    # --- Rule #1 -----------------------------------------------------------------------------

    def untagged_orders(self) -> list[dict]:
        """Today's orders in the account that are not ours (no 'tl' reference)."""
        return [r for r in self._all_orders() if not g.is_ours(r.get("order_reference_id"))]

    def cancel_orphans(self) -> list[dict]:
        """Our own orders still working at Groww that this process isn't tracking: sent by an earlier run
        that died. Their fills would never reach the ledger, so they are cancelled (and reported); the
        strategy sends a fresh order if it still wants one. Anything they filled before this shows up
        as a position the ledger lacks, which startup reconciliation catches."""
        known = {t.reference for t in self._tracked.values()}
        out = []
        for r in self._all_orders():
            ref = r.get("order_reference_id")
            if not g.is_ours(ref) or ref in known:
                continue
            try:
                working = not g.order_status(r.get("order_status"), int(r.get("filled_quantity") or 0),
                                             int(r.get("quantity") or 0)).is_final
            except g.UnknownStatus:
                working = True                                   # can't tell: cancelling is the safe side
            if working and r.get("groww_order_id"):
                log.error("ALERT order %s from an earlier run is still working at Groww: cancelling it", ref)
                self._cancel_id(r["groww_order_id"])
                out.append(r)
        return out

    # --- internals ---------------------------------------------------------------------------

    def _send(self, t: _Tracked, params: dict) -> None:
        for attempt in range(1, self._retries + 1):
            try:
                resp = self.api.place_order(**params, timeout=self._timeout)
            except Exception as e:
                kind = g.classify_error(e)
                if kind == "retry" and attempt < self._retries:
                    self._sleep(attempt)                         # rate limited: nothing was accepted
                    continue
                if kind in ("retry", "rejected", "auth"):
                    if kind == "auth":
                        log.error("ALERT Groww refused our credentials or permissions: %s", e)
                    return self._finish(t, OrderStatus.REJECTED, f"{type(e).__name__}: {e}")
                log.error("ALERT %s: order %s may or may not have reached Groww; not sending again", type(e).__name__, t.reference)
                try:
                    raw = self._lookup(t)
                except LookupFailed:
                    return
                if raw is not None:
                    self._apply(t, raw)
                return
            t.groww_id = resp.get("groww_order_id")
            return self._apply(t, {**resp, "quantity": t.order.qty})

    def _lookup(self, t: _Tracked) -> dict | None:
        """The order Groww holds under this reference, or None if it holds none."""
        try:
            raw = self.api.get_order_status_by_reference(segment=g.SEGMENT, order_reference_id=t.reference, timeout=self._timeout)
        except Exception as e:
            if g.is_not_found(e):
                return None
            raise LookupFailed(f"{type(e).__name__}: {e}") from e
        if not raw.get("groww_order_id"):                        # the status call may omit the id: find it in the list
            raw = {**raw, "groww_order_id": next((r.get("groww_order_id") for r in self._all_orders()
                                                  if r.get("order_reference_id") == t.reference), None)}
        return raw

    def _apply(self, t: _Tracked, raw: dict) -> None:
        if raw.get("groww_order_id"):
            t.groww_id = raw["groww_order_id"]
        now = self._clock()
        upd = g.order_update(raw, t.order.client_ref, now, qty=t.order.qty)
        if upd.filled_qty > t.filled and not upd.avg_price and t.groww_id:
            try:                                                 # the status calls omit the average price; the detail has it
                detail = self.api.get_order_detail(segment=g.SEGMENT, groww_order_id=t.groww_id, timeout=self._timeout)
                upd = g.order_update(detail, t.order.client_ref, now, qty=t.order.qty)
            except Exception as e:
                log.warning("no average fill price for %s yet (%s); will retry", t.reference, e)
                return
        if upd.filled_qty > t.filled:
            fill = self._fills.update(t.order.client_ref, t.order.instrument, t.order.side, upd.filled_qty, upd.avg_price, now)
            if fill:
                for h in self._fill_handlers:
                    h(fill)
        if (upd.status, upd.filled_qty) != (t.status, t.filled):
            t.status, t.filled = upd.status, upd.filled_qty
            self._emit(upd)
        t.final = upd.status.is_final
        if t.cancel_when_found and not t.final and t.groww_id:
            t.cancel_when_found = False
            self._cancel_id(t.groww_id)

    def _cancel_id(self, groww_id: str) -> None:
        try:
            self.api.cancel_order(groww_order_id=groww_id, segment=g.SEGMENT, timeout=self._timeout)
        except Exception as e:
            if g.classify_error(e) in ("rejected",):
                log.warning("cancel of %s refused (probably already done): %s", groww_id, e)
            else:
                log.error("ALERT cancel of %s failed: %s %s", groww_id, type(e).__name__, e)

    def _all_orders(self) -> list[dict]:
        out, seen = [], set()
        for page in range(MAX_PAGES):
            rows = _listing(self.api.get_order_list(page=page, segment=g.SEGMENT, timeout=self._timeout), ("order_list", "orders"))
            ids = {r.get("groww_order_id") for r in rows}
            if not rows or ids <= seen:                          # empty, or the server ignored the page number
                break
            seen |= ids
            out += rows
        return out

    def _refuse(self, order: Order, status: OrderStatus, message: str) -> None:
        log.warning("ALERT order refused: %s", message)
        self._tracked[order.client_ref] = _Tracked(order, g.reference_id(order.client_ref), final=True)
        self._emit(OrderUpdate(order.client_ref, status, self._clock(), message=message))

    def _finish(self, t: _Tracked, status: OrderStatus, message: str) -> None:
        t.final, t.status = True, status
        self._emit(OrderUpdate(t.order.client_ref, status, self._clock(), broker_order_id=t.groww_id, message=message))

    def _emit(self, u: OrderUpdate) -> None:
        for h in self._update_handlers:
            h(u)

    # --- restart -----------------------------------------------------------------------------

    def resume(self, order: Order, filled_qty: int = 0, avg_price: float = 0.0) -> None:
        """Pick up an order sent before a restart (from the order store): poll() finds it by its
        reference. Fills already booked are passed in so they are not emitted again."""
        with self._lock:
            self._tracked[order.client_ref] = _Tracked(order, g.reference_id(order.client_ref), sent_ms=self._clock(),
                                                       filled=filled_qty)
            if filled_qty:
                self._fills.seed(order.client_ref, filled_qty, avg_price)
