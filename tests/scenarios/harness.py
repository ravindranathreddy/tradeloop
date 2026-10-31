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


md = FakeMD()
import tradeloop.auth as auth  # noqa: E402
from tradeloop.store import Store  # noqa: E402

auth.login = lambda: object()
rec_mod.build = lambda cfg, api, today: (rec_mod.Recorder(cfg, md, Store(cfg["db_path"]), today), md)

if SPEC.get("stray"):
    from tradeloop.paper_broker import PaperBroker
    _load = PaperBroker.load_positions
    PaperBroker.load_positions = lambda self, pos: _load(self, {**pos, STRAY: 65})

cfg = yaml.safe_load((W / "config.yaml").read_text())
sys.argv = ["recorder", "--config", str(W / "config.yaml")]
rec_mod.main()
print("DONE")
