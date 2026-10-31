"""Child process: one simulated trading day through the real recorder.main(), fake feed.

    python harness.py WORKDIR '{"date": "2026-11-02", "until": "10:00", ...}'

Time runs 60x (one real second = one market minute) by patching time.time and the
recorder's datetime.now. The fake feed publishes NIFTY and a strip of puts expiring
2026-11-03 every 6 simulated seconds, with a 5-level book on each. Day spec keys:

  date, until            simulated day and the HH:MM the feed stops
  silent: [from, to]     feed publishes nothing in this window
  moves: [[hm, index, {strike: ltp}], ...]   from hm on, the index and these option prices change
  no_book: [strike]      options with a price but no order book
  crash_at               HH:MM: the process dies abruptly (os._exit), as a crash would
  cli_kill_at            HH:MM: `python -m tradeloop.kill_switch kill` is run, as Ranga would
  stray                  the paper broker also holds a position the ledger never booked

Live drills add `groww: true`: the Groww API is a stateful fake exchange (kept in WORKDIR/groww.json
across days) that fills orders against the fake feed's order book. More keys:

  groww_outage: [from, to]   every Groww call fails in this window
  groww_stray: {strike: qty} Groww holds a position the ledger never booked
  groww_foreign_order        Groww holds an order that did not come from the bot
  groww_timeout_lands        the first order's response times out, though Groww has the order
"""
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, time as dtime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from tradeloop.core.ids import InstrumentId  # noqa: E402
from tradeloop.core.market_calendar import IST  # noqa: E402
from tradeloop.core.types import Contract, Level, Tick  # noqa: E402

W, SPEC = Path(sys.argv[1]), json.loads(sys.argv[2])
SPEED = 60
BASE = datetime.combine(date.fromisoformat(SPEC["date"]), dtime(9, 5), IST).timestamp()
T0 = time.monotonic()
sim = lambda: BASE + (time.monotonic() - T0) * SPEED
time.time = sim

import tradeloop.recorder as rec_mod  # noqa: E402


class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(sim(), tz)


rec_mod.datetime = FakeDT

EXP = date(2026, 11, 3)
NIFTY = InstrumentId.index("NSE", "NIFTY")
STRIKES = list(range(23800, 24700, 100))
OPTS = {k: InstrumentId.option("NSE", "NIFTY", EXP, k, "PE") for k in STRIKES}
STRAY = InstrumentId.option("NSE", "NIFTY", EXP, 20000.0, "PE")


class FakeMD:
    def __init__(self):
        self.handlers, self.stopped, self.killed = [], False, False

    def instruments(self): return [Contract(o, 65) for o in OPTS.values()]
    def subscribe(self, ids): pass
    def on_tick(self, h): self.handlers.append(h)
    def stop(self): self.stopped = True

    def push(self, t):
        for h in self.handlers:
            h(t)

    def run(self):
        silent, crash, cli = SPEC.get("silent"), SPEC.get("crash_at"), SPEC.get("cli_kill_at")
        while not self.stopped:
            now = sim()
            hm = f"{datetime.fromtimestamp(now, IST):%H:%M}"
            if hm >= SPEC["until"]:
                break
            if crash and hm >= crash:
                os._exit(137)
            if cli and not self.killed and hm >= cli:
                self.killed = True
                subprocess.run([sys.executable, "-m", "tradeloop.kill_switch", "kill", "--config", str(W / "config.yaml"),
                                "--reason", "drill"], cwd=ROOT, capture_output=True)
            if not (silent and silent[0] <= hm < silent[1]):
                self.publish(now, hm)
            time.sleep(0.1)

    def publish(self, now, hm):
        ms = int(now * 1000)
        index, override = 25000.0, {}
        for at, idx, prices in SPEC.get("moves", []):
            if hm >= at:
                index, override = idx, {int(k): v for k, v in prices.items()}
        self.push(Tick(NIFTY, ms, ltp=index, recv_ms=ms))
        for k, o in OPTS.items():
            p = override.get(k, max(0.5, 5 + (k - 23900) * 0.03))
            self.push(Tick(o, ms, ltp=p, recv_ms=ms))
            if k not in SPEC.get("no_book", []):
                self.push(Tick(o, ms, recv_ms=ms, bid=p - .2, ask=p + .2,
                               bids=tuple(Level(round(p - .2 - .1 * i, 2), 650.0) for i in range(5)),
                               asks=tuple(Level(round(p + .2 + .1 * i, 2), 650.0) for i in range(5))))
                if EX:
                    EX.on_book(TS[k], round(p - .2, 2), round(p + .2, 2))


EX = None
if SPEC.get("groww"):
    import pandas as pd  # noqa: E402

    from tests.test_groww_execution import FakeGroww, Timeout  # noqa: E402
    from tradeloop.brokers import groww_orders as g  # noqa: E402

    TS = {k: f"NIFTY26N03{k}PE" for k in [*STRIKES, 20000]}
    SYMBOLS = g.symbols_from_rows(pd.DataFrame([
        {"exchange": "NSE", "segment": "FNO", "exchange_token": str(k), "trading_symbol": name, "instrument_type": "PE",
         "underlying_symbol": "NIFTY", "expiry_date": "2026-11-03", "strike_price": float(k), "lot_size": 65.0,
         "tick_size": 0.05, "freeze_quantity": 1755.0, "buy_allowed": 1, "sell_allowed": 1} for k, name in TS.items()]))
    STATE = W / "groww.json"

    def hm_now():
        return f"{datetime.fromtimestamp(sim(), IST):%H:%M}"

    class Exchange(FakeGroww):
        """Groww as a fake exchange: orders fill against the feed's best bid/ask; state survives restarts."""

        def __init__(self):
            super().__init__(page_size=50)
            self.placed, self.books = [], {}
            if STATE.exists():
                d = json.loads(STATE.read_text())
                self.orders, self.net, self.next_id, self.placed = d["orders"], d["net"], d["next_id"], d["placed"]
            if SPEC.get("groww_timeout_lands"):
                self.place_errors = [Timeout()]

        def save(self):
            STATE.write_text(json.dumps({"orders": self.orders, "net": self.net, "next_id": self.next_id, "placed": self.placed}))

        def _down(self):
            w = SPEC.get("groww_outage")
            if w and w[0] <= hm_now() < w[1]:
                raise ConnectionError("groww unreachable")

        def place_order(self, **kw):
            self._down()
            self.placed.append({"hm": hm_now(), "symbol": kw["trading_symbol"], "side": kw["transaction_type"],
                                "type": kw["order_type"], "price": kw.get("price"), "ref": kw["order_reference_id"]})
            try:
                return super().place_order(**kw)
            finally:
                o = self._find(kw["order_reference_id"])
                if o:
                    o["price"], o["order_type"] = kw.get("price"), kw["order_type"]
                    self._match(o)
                self.save()

        def _match(self, o):
            book = self.books.get(o["trading_symbol"])
            if not book or o["order_status"] != "ACKED" or o["filled_quantity"]:
                return
            bid, ask = book
            if o["transaction_type"] == "BUY" and (o["order_type"] == "MARKET" or o["price"] >= ask):
                self.fill(o["order_reference_id"], o["quantity"], ask)
            elif o["transaction_type"] == "SELL" and (o["order_type"] == "MARKET" or o["price"] <= bid):
                self.fill(o["order_reference_id"], o["quantity"], bid)

        def on_book(self, symbol, bid, ask):
            self.books[symbol] = (bid, ask)
            for o in list(self.orders.values()):
                if o["trading_symbol"] == symbol:
                    self._match(o)
            self.save()

        def get_order_status_by_reference(self, **kw):
            self._down()
            return super().get_order_status_by_reference(**kw)

        def get_order_detail(self, **kw):
            self._down()
            return super().get_order_detail(**kw)

        def cancel_order(self, **kw):
            self._down()
            try:
                return super().cancel_order(**kw)
            finally:
                self.save()

        def get_order_list(self, **kw):
            self._down()
            return super().get_order_list(**kw)

        def get_positions_for_user(self, **kw):
            self._down()
            return super().get_positions_for_user(**kw)

    EX = Exchange()
    for k, q in SPEC.get("groww_stray", {}).items():
        EX.net[TS[int(k)]] = EX.net.get(TS[int(k)], 0) + q
    if SPEC.get("groww_foreign_order"):
        EX._create({"order_reference_id": "manualorder1", "quantity": 65, "trading_symbol": TS[24000],
                    "transaction_type": "BUY"})
    EX.save()

md = FakeMD()
import tradeloop.auth as auth  # noqa: E402
from tradeloop.store import Store  # noqa: E402

auth.login = lambda: EX or object()
if EX:
    rec_mod.load_symbols = lambda api: SYMBOLS
rec_mod.build = lambda cfg, api, today: (rec_mod.Recorder(cfg, md, Store(cfg["db_path"]), today), md)

if SPEC.get("stray"):
    from tradeloop.paper_broker import PaperBroker
    _load = PaperBroker.load_positions
    PaperBroker.load_positions = lambda self, pos: _load(self, {**pos, STRAY: 65})

cfg = yaml.safe_load((W / "config.yaml").read_text())
sys.argv = ["recorder", "--config", str(W / "config.yaml")]
rec_mod.main()
print("DONE")
