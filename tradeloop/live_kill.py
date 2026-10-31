"""Standalone live kill: flatten the Groww account from the command line, with no recorder alive.

    python -m tradeloop.kill_switch flatten-live --yes

The recorder flattens live positions itself when the kill file appears (PaperTrader + LiveBroker,
with the watchdog thread driving it even if the feed is dead). This is for when the recorder is not
running at all. It works from Groww's own positions rather than the ledger, cancels every working
order, buys shorts back first and sells protection only once no short remains (`flatten_step`).
It is armed on purpose: Ranga typed --yes.

If the recorder's heartbeat is fresh it refuses, since two processes sending exits could flip the
position the other way. It halts the strategies first (the kill file), so a recorder started
afterwards stays halted until `kill_switch resume`.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from tradeloop.core.types import Position
from tradeloop.kill_switch import flatten_step, request
from tradeloop.live_broker import LiveBroker

log = logging.getLogger("live_kill")


def recorder_running(heartbeat_file: Path, now_ms: int, max_age_s: float = 30) -> bool:
    try:
        return (now_ms - int(Path(heartbeat_file).read_text())) / 1000 <= max_age_s
    except (FileNotFoundError, ValueError):
        return False


def flatten_live(broker: LiveBroker, kill_file: Path, *, reason: str = "manual flatten-live",
                 prices_for: Callable[[list[Position]], dict] = lambda positions: {},
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Callable[[float], None] = time.sleep, max_seconds: float = 180,
                 interval: float = 1.0) -> list[Position]:
    """Returns the positions still open when it stops (empty when flat)."""
    ex = broker.execution
    start = clock_ms()
    request(kill_file, reason, start, flatten=True)
    ex.cancel_all()
    positions = ex.positions()
    broker.seed_prices(prices_for(positions))
    attempts: dict = {}
    while True:
        ex.poll()
        positions = ex.positions()
        if not positions:
            log.warning("ALERT live kill: the account is flat")
            return []
        if clock_ms() - start > max_seconds * 1000:
            log.error("ALERT live kill: still holding %s after %ds", ", ".join(f"{p.qty:+d} {p.instrument}" for p in positions),
                      max_seconds)
            return positions
        flatten_step(broker, positions, ex.working(), strategy_id="kill_switch", config_version="manual",
                     request_ms=start, attempts=attempts)
        broker.tend()
        sleep(interval)


def main(cfg: dict, *, confirmed: bool, even_if_running: bool) -> int:
    from tradeloop.auth import login
    from tradeloop.brokers.groww_execution import GrowwExecution
    from tradeloop.groww_smoke import last_prices
    from tradeloop.recorder import load_symbols

    if not confirmed:
        print("This sends REAL exit orders for every position in the Groww account. Run again with --yes.")
        return 2
    wd = cfg.get("watchdog", {})
    if not even_if_running and recorder_running(Path(wd.get("heartbeat_file", "data/heartbeat")), int(time.time() * 1000)):
        print("The recorder is running and flattens live positions itself: check `kill_switch status` first. "
              "Use --even-if-running only if it is stuck.")
        return 2
    api = login()
    ex = GrowwExecution(api, load_symbols(api), armed=True)
    broker = LiveBroker(ex, ex._symbols)
    left = flatten_live(broker, Path(cfg.get("kill_file", "data/KILL")),
                        prices_for=lambda ps: last_prices([p.instrument for p in ps]))
    print("Flat." if not left else "STILL OPEN: " + ", ".join(f"{p.qty:+d} {p.instrument}" for p in left))
    return 0 if not left else 1
