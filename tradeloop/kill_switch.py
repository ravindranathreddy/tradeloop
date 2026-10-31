"""Kill switch (design rule 13): cancel everything, then flatten. Exits only.

    python -m tradeloop.kill_switch kill [--reason "..."]   halt, cancel all, flatten
    python -m tradeloop.kill_switch status
    python -m tradeloop.kill_switch resume                  lift the halt (after you've looked)
    python -m tradeloop.kill_switch flatten-live --yes      recorder not running: flatten the Groww account

The command writes a request file (data/KILL). While it exists the risk gate rejects every
strategy order (nothing new, nothing re-opened) and the paper trader cancels what is working and
flattens its positions. Flatten orders go straight to the broker, not through the gate or the
strategy. `flatten_step()` is broker-agnostic: with the Groww Execution adapter the same function
runs from this command with no engine alive. Until then the paper broker lives inside the
recorder, so the recorder must be running for a paper flatten to fill.

Shorts are bought back first and the protection is sold only after that is confirmed, so a spread
never sits with its hedge gone and a short naked.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from tradeloop.core.interfaces import Execution
from tradeloop.core.types import Order, OrderType, Position, Product, Side

log = logging.getLogger("kill_switch")
KILL_FILE = Path("data/KILL")


def request(path: Path = KILL_FILE, reason: str = "manual", now_ms: int | None = None, *, flatten: bool = True) -> dict:
    """flatten=False halts only: no new orders, positions left as they are (used when we don't
    know which side is right, e.g. the ledger and the broker disagree)."""
    req = {"requested_ms": now_ms or int(time.time() * 1000), "reason": reason, "flatten": flatten}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(req))
    return req


def active(path: Path = KILL_FILE) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except ValueError:                      # unreadable file still halts: fail safe
        return {"requested_ms": 0, "reason": "unreadable kill file"}


def clear(path: Path = KILL_FILE) -> None:
    Path(path).unlink(missing_ok=True)


def flatten_step(execution: Execution, positions: list[Position], working: list[Order], *, strategy_id: str,
                 config_version: str, request_ms: int, attempts: dict) -> list[Order]:
    """One step towards flat; call it repeatedly (about once a second) until `positions` is empty.

    Shorts are bought back first. Longs (the protection) are sold only once no short remains in
    `positions`, which come from confirmed fills: two orders sent together fill independently and
    the sale can win, leaving a naked short for a moment. An instrument with a working order is
    left alone; an order that lapsed unfilled is sent again under a new client_ref."""
    busy = {o.instrument for o in working}
    shorts = [p for p in positions if p.qty < 0]
    todo = shorts or [p for p in positions if p.qty > 0]
    sent = []
    for p in sorted(todo, key=lambda p: str(p.instrument)):
        if p.instrument in busy:
            continue
        n = attempts[p.instrument] = attempts.get(p.instrument, 0) + 1
        side = Side.BUY if p.qty < 0 else Side.SELL
        order = Order(p.instrument, side, abs(p.qty), OrderType.MARKET, Product.CARRY, strategy_id, config_version,
                      client_ref=f"kill-{request_ms}-{strategy_id}-{p.instrument}-{n}")
        execution.place(order)
        sent.append(order)
        log.warning("ALERT kill switch: %s %d %s", side.value, order.qty, p.instrument)
    return sent


def main():
    import yaml

    from tradeloop.paper_store import PaperStore

    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["kill", "status", "resume", "flatten-live"])
    p.add_argument("--yes", action="store_true", help="flatten-live: really send the exit orders")
    p.add_argument("--even-if-running", action="store_true", help="flatten-live: even though the recorder looks alive")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--reason", default="manual")
    a = p.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())
    path = Path(cfg.get("kill_file", KILL_FILE))
    if a.command == "flatten-live":
        import sys

        from tradeloop.live_kill import main as flatten_live_main

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        sys.exit(flatten_live_main(cfg, confirmed=a.yes, even_if_running=a.even_if_running))
    if a.command == "kill":
        request(path, a.reason)
        print(f"Kill requested ({a.reason}). Orders are blocked now; the recorder flattens paper "
              f"positions within seconds. Check: python -m tradeloop.kill_switch status")
    elif a.command == "resume":
        clear(path)
        print("Halt lifted. Strategies may trade again.")
    req = active(path)
    print("HALTED: " + json.dumps(req) if req else "Not halted.")
    store = PaperStore(cfg.get("paper_db", "data/paper.db"))
    open_now = []
    for pth in cfg.get("paper", []):
        sid = yaml.safe_load(Path(pth).read_text())["strategy_id"]
        open_now += [f"  {sid}: {pos.qty:+d} {pos.instrument}" for pos in store.rebuild_ledger(sid).positions()]
    print("Open paper positions:" + ("\n" + "\n".join(open_now) if open_now else " none"))


if __name__ == "__main__":
    main()
