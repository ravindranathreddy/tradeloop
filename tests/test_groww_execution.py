"""Groww Execution adapter against a fake API (no network, no real orders)."""
import pytest

from tests.test_groww_orders import LONG, SHORT, SYMS, order
from tradeloop.brokers import groww_orders as g
from tradeloop.brokers.groww_execution import GrowwExecution, UnexpectedResponse
from tradeloop.core.interfaces import Execution
from tradeloop.core.types import OrderStatus, Side
from tradeloop.kill_switch import flatten_step

err = lambda name, **attrs: type(name, (Exception,), attrs)
NotFound, Timeout = err("GrowwAPINotFoundException"), err("GrowwAPITimeoutException", lands=True)
LostTimeout, RateLimit = err("GrowwAPITimeoutException", lands=False), err("GrowwAPIRateLimitException")
BadRequest, Boom = err("GrowwAPIBadRequestException"), err("ConnectionResetError")


class FakeGroww:
    def __init__(self, page_size=2):
        self.orders, self.calls, self.place_errors, self.page_size = {}, [], [], page_size
        self.lookup_error, self.status_has_id, self.next_id = None, True, 1
        self.net = {}                                                    # trading_symbol -> signed quantity

    def _create(self, kw):
        gid = f"GMK{self.next_id}"
        self.next_id += 1
        self.orders[gid] = {"groww_order_id": gid, "order_reference_id": kw["order_reference_id"], "order_status": "ACKED",
                            "quantity": kw["quantity"], "filled_quantity": 0, "average_fill_price": 0.0,
                            "trading_symbol": kw["trading_symbol"], "transaction_type": kw["transaction_type"]}
        return gid

    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        if self.place_errors:
            e = self.place_errors.pop(0)
            if getattr(e, "lands", False):
                self._create(kw)
            raise e
        return {"groww_order_id": self._create(kw), "order_status": "NEW", "order_reference_id": kw["order_reference_id"], "remark": ""}

    def _find(self, ref):
        return next((o for o in self.orders.values() if o["order_reference_id"] == ref), None)

    def get_order_status_by_reference(self, **kw):
        self.calls.append(("status_by_reference", kw))
        if self.lookup_error:
            raise self.lookup_error
        o = self._find(kw["order_reference_id"])
        if o is None:
            raise NotFound()
        out = {k: o[k] for k in ("order_status", "order_reference_id")} | {"filled_quantity": o["filled_quantity"], "remark": ""}
        return out | ({"groww_order_id": o["groww_order_id"]} if self.status_has_id else {})

    def get_order_detail(self, **kw):
        self.calls.append(("order_detail", kw))
        return dict(self.orders[kw["groww_order_id"]])

    def cancel_order(self, **kw):
        self.calls.append(("cancel_order", kw))
        o = self.orders[kw["groww_order_id"]]
        if o["order_status"] in ("NEW", "ACKED", "APPROVED"):
            o["order_status"] = "CANCELLED"
        return {"groww_order_id": o["groww_order_id"], "order_status": o["order_status"]}

    def get_order_list(self, **kw):
        self.calls.append(("order_list", kw))
        rows = sorted(self.orders.values(), key=lambda o: o["groww_order_id"])
        return {"order_list": rows[kw["page"] * self.page_size:(kw["page"] + 1) * self.page_size]}

    def get_positions_for_user(self, **kw):
        self.calls.append(("positions", kw))
        return {"positions": [{"trading_symbol": s, "quantity": q, "net_price": 10.0, "credit_quantity": max(q, 0),
                               "debit_quantity": max(-q, 0)} for s, q in self.net.items() if q]}

    def fill(self, ref, qty, avg):
        o = self._find(ref)
        sign = 1 if o["transaction_type"] == "BUY" else -1
        self.net[o["trading_symbol"]] = self.net.get(o["trading_symbol"], 0) + sign * (qty - o["filled_quantity"])
        o["filled_quantity"], o["average_fill_price"] = qty, avg
        o["order_status"] = "EXECUTED" if qty == o["quantity"] else "APPROVED"


class Clock:
    now = 1_000_000

    def __call__(self):
        return self.now


@pytest.fixture
def rig():
    api, clk, sleeps = FakeGroww(), Clock(), []
    ex = GrowwExecution(api, SYMS, armed=True, clock_ms=clk, sleep=sleeps.append)
    ex.updates, ex.fills = [], []
    ex.on_order_update(ex.updates.append)
    ex.on_fill(ex.fills.append)
    ex.api_, ex.clk, ex.sleeps = api, clk, sleeps
    return ex


def placed(api):
    return [kw for name, kw in api.calls if name == "place_order"]


def ref(client_ref):
    return g.reference_id(client_ref)


# --- basics -----------------------------------------------------------------------------------

def test_is_an_execution_and_refuses_everything_until_armed():
    api = FakeGroww()
    ex = GrowwExecution(api, SYMS)
    updates = []
    ex.on_order_update(updates.append)
    ex.place(order())
    assert isinstance(ex, Execution) and api.calls == []
    assert [(u.status, "not armed" in u.message) for u in updates] == [(OrderStatus.REJECTED_RISK, True)]


def test_a_filled_order_emits_the_fill_before_the_status(rig):
    seen = []
    rig.on_fill(lambda f: seen.append("fill"))
    rig.on_order_update(lambda u: seen.append(u.status.value))
    rig.place(order(ref="r1"))
    assert [u.status for u in rig.updates] == [OrderStatus.SENT, OrderStatus.OPEN]
    rig.api_.fill(ref("r1"), 65, 19.8)
    rig.poll()
    rig.poll()                                                           # nothing new the second time
    assert seen[-2:] == ["fill", "FILLED"] and len(rig.fills) == 1
    f = rig.fills[0]
    assert (f.instrument, f.side, f.qty, f.price, f.client_ref) == (SHORT, Side.SELL, 65, 19.8, "r1")
    assert rig.working() == []


def test_partial_fills_arrive_as_slices_at_the_right_average(rig):
    rig.place(order(qty=130, ref="r1"))
    rig.api_.fill(ref("r1"), 65, 20.0)
    rig.poll()
    rig.api_.fill(ref("r1"), 130, 19.5)
    rig.poll()
    assert [(f.qty, f.price) for f in rig.fills] == [(65, 20.0), (65, 19.0)]
    assert [u.status for u in rig.updates][-2:] == [OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED]


def test_every_call_carries_a_timeout_and_the_same_order_is_never_sent_twice(rig):
    rig.place(order(ref="r1"))
    rig.place(order(ref="r1"))
    rig.poll()
    assert len(placed(rig.api_)) == 1
    assert all(kw.get("timeout") == 10 for _, kw in rig.api_.calls)


def test_orders_failing_our_own_checks_never_reach_groww(rig):
    rig.place(order(qty=60, ref="r1"))
    assert rig.api_.calls == [] and rig.updates[-1].status is OrderStatus.REJECTED_RISK


# --- the dangerous part: not knowing whether an order went through ------------------------------------

def test_an_order_groww_already_holds_is_adopted_not_sent_again(rig):
    rig.place(order(ref="r1"))
    rig.api_.fill(ref("r1"), 65, 19.8)
    fresh = GrowwExecution(rig.api_, SYMS, armed=True, clock_ms=rig.clk, sleep=rig.sleeps.append)   # a restart
    fills = []
    fresh.on_fill(fills.append)
    fresh.place(order(ref="r1"))
    assert len(placed(rig.api_)) == 1 and [(f.qty, f.price) for f in fills] == [(65, 19.8)]


def test_if_groww_cannot_be_asked_the_order_is_refused_not_sent(rig):
    rig.api_.lookup_error = Boom()
    rig.place(order(ref="r1"))
    assert placed(rig.api_) == [] and rig.updates[-1].status is OrderStatus.REJECTED


def test_a_timeout_after_the_order_landed_adopts_it_and_does_not_resend(rig):
    rig.api_.place_errors = [Timeout()]
    rig.place(order(ref="r1"))
    assert len(placed(rig.api_)) == 1 and rig.working() != []
    assert rig.updates[-1].status is OrderStatus.OPEN and rig.updates[-1].broker_order_id == "GMK1"


def test_a_timeout_where_nothing_is_found_waits_then_gives_up_and_never_resends(rig):
    rig.api_.place_errors = [LostTimeout()]
    rig.place(order(ref="r1"))
    rig.poll()
    assert rig.working() != [] and len(placed(rig.api_)) == 1             # still unverified inside the grace period
    rig.clk.now += 31_000
    rig.poll()
    assert rig.working() == [] and rig.updates[-1].status is OrderStatus.REJECTED
    assert len(placed(rig.api_)) == 1


def test_a_late_arriving_order_is_adopted_during_the_grace_period(rig):
    rig.api_.place_errors = [LostTimeout()]
    rig.place(order(ref="r1"))
    rig.api_._create(placed(rig.api_)[0])                                 # it reaches Groww after all
    rig.poll()
    assert rig.updates[-1].status is OrderStatus.OPEN and len(placed(rig.api_)) == 1


def test_rate_limits_are_retried_with_the_same_reference_and_then_given_up(rig):
    rig.api_.place_errors = [RateLimit(), RateLimit()]
    rig.place(order(ref="r1"))
    refs = {kw["order_reference_id"] for kw in placed(rig.api_)}
    assert len(placed(rig.api_)) == 3 and refs == {ref("r1")} and rig.sleeps == [1, 2]
    rig.api_.place_errors = [RateLimit(), RateLimit(), RateLimit()]
    rig.place(order(ref="r2"))
    assert rig.updates[-1].status is OrderStatus.REJECTED and len(placed(rig.api_)) == 6


def test_a_refused_request_is_final_and_not_retried(rig):
    rig.api_.place_errors = [BadRequest("insufficient margin")]
    rig.place(order(ref="r1"))
    assert len(placed(rig.api_)) == 1 and rig.updates[-1].status is OrderStatus.REJECTED and rig.working() == []


def test_the_status_call_without_an_id_or_average_price_still_works(rig):
    rig.api_.place_errors = [Timeout()]
    rig.api_.status_has_id = False                                        # the id must be found in the order list
    rig.place(order(ref="r1"))
    rig.api_.fill(ref("r1"), 65, 19.8)
    rig.poll()
    assert [(f.qty, f.price) for f in rig.fills] == [(65, 19.8)]


# --- cancel -----------------------------------------------------------------------------------

def test_cancel_goes_to_groww_and_the_next_poll_reports_it(rig):
    rig.place(order(ref="r1"))
    rig.cancel("r1")
    rig.poll()
    assert [kw["groww_order_id"] for n, kw in rig.api_.calls if n == "cancel_order"] == ["GMK1"]
    assert rig.updates[-1].status is OrderStatus.CANCELLED and rig.working() == []


def test_cancelling_an_unverified_order_happens_the_moment_it_shows_up(rig):
    rig.api_.place_errors = [LostTimeout()]
    rig.place(order(ref="r1"))
    rig.cancel("r1")
    rig.api_._create(placed(rig.api_)[0])
    rig.poll()
    rig.poll()
    assert rig.updates[-1].status is OrderStatus.CANCELLED


def test_cancel_all_reads_every_page_and_cancels_foreign_orders_too(rig, caplog):
    for i in range(3):
        rig.place(order(ref=f"r{i}"))
    foreign = rig.api_._create({"order_reference_id": "12345678", "quantity": 65, "trading_symbol": "X", "transaction_type": "BUY"})
    rig.api_.fill(ref("r0"), 65, 19.8)                                    # already executed: left alone
    rig.cancel_all()
    status = {o["groww_order_id"]: o["order_status"] for o in rig.api_.orders.values()}
    assert status == {"GMK1": "EXECUTED", "GMK2": "CANCELLED", "GMK3": "CANCELLED", foreign: "CANCELLED"}
    assert "not ours" in caplog.text


# --- positions and Rule #1 --------------------------------------------------------------------------

def test_positions_are_read_and_an_unknown_contract_fails_loudly(rig):
    rig.api_.net = {"NIFTY26N0324400PE": -65, "NIFTY26N0323900PE": 65}
    assert {(p.instrument, p.qty) for p in rig.positions()} == {(SHORT, -65), (LONG, 65)}
    rig.api_.net["RELIANCE26NFUT"] = 250
    with pytest.raises(g.UnknownSymbol):
        rig.positions()


def test_an_unrecognisable_list_response_is_refused(rig):
    rig.api_.get_positions_for_user = lambda **kw: {"status": "ok", "a": [1], "b": [2]}
    with pytest.raises(UnexpectedResponse):
        rig.positions()


def test_orders_without_our_prefix_are_listed_as_untagged(rig):
    rig.place(order(ref="r1"))
    rig.api_._create({"order_reference_id": "98765432", "quantity": 65, "trading_symbol": "X", "transaction_type": "BUY"})
    assert [o["order_reference_id"] for o in rig.untagged_orders()] == ["98765432"]


# --- restart and the kill switch ------------------------------------------------------------------

def test_resumed_orders_do_not_replay_fills_already_booked(rig):
    rig.place(order(qty=130, ref="r1"))
    rig.api_.fill(ref("r1"), 65, 20.0)
    fresh = GrowwExecution(rig.api_, SYMS, armed=True, clock_ms=rig.clk, sleep=rig.sleeps.append)
    fills = []
    fresh.on_fill(fills.append)
    fresh.resume(order(qty=130, ref="r1"), filled_qty=65, avg_price=20.0)
    fresh.poll()
    assert fills == []
    rig.api_.fill(ref("r1"), 130, 19.5)
    fresh.poll()
    assert [(f.qty, f.price) for f in fills] == [(65, 19.0)]


def test_the_kill_switch_flattens_through_groww_shorts_first_and_only_on_confirmed_fills(rig):
    rig.api_.net = {"NIFTY26N0324400PE": -65, "NIFTY26N0323900PE": 65}
    attempts = {}
    for _ in range(6):
        pos = rig.positions()
        if not pos:
            break
        flatten_step(rig, pos, rig.working(), strategy_id="pcs", config_version="v", request_ms=1, attempts=attempts)
        for o in list(rig.api_.orders.values()):
            if o["filled_quantity"] == 0:
                rig.api_.fill(o["order_reference_id"], o["quantity"], 10.0)
        rig.poll()
    sent = [(kw["transaction_type"], kw["trading_symbol"]) for kw in placed(rig.api_)]
    assert sent == [("BUY", "NIFTY26N0324400PE"), ("SELL", "NIFTY26N0323900PE")]     # the sale waited for the buy-back
    assert rig.positions() == []
