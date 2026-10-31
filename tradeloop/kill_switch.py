"""Kill switch (design rule 13): cancel everything, then flatten. Exits only.

    python -m tradeloop.kill_switch kill [--reason "..."]   halt, cancel all, flatten
    python -m tradeloop.kill_switch status
    python -m tradeloop.kill_switch resume                  lift the halt (after you've looked)

The command writes a request file (data/KILL). While it exists the risk gate rejects every
strategy order (nothing new, nothing re-opened) and the paper trader flattens its positions
at the next tick. Flatten orders go straight to the broker, not through the gate or the
strategy. `flatten()` is broker-agnostic: with the Groww Execution adapter the same function
runs from this command with no engine alive. Until then the paper broker lives inside the
recorder, so the recorder must be running for a paper flatten to fill.

Buy-backs of shorts go out before sales of longs, so a spread never sits with its hedge gone.
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


def flatten(execution: Execution, positions: list[Position], *, strategy_id: str, config_version: str,
            request_ms: int) -> list[Order]:
    """Cancel all orders, then close every position with market orders. The client_ref is
    fixed by the request, so running the same request twice never sends a second set."""
    execution.cancel_all()
    todo = sorted((p for p in positions if p.qty), key=lambda p: (p.qty > 0, str(p.instrument)))   # shorts first
    sent = []
    for n, p in enumerate(todo):
        side = Side.BUY if p.qty < 0 else Side.SELL
        order = Order(p.instrument, side, abs(p.qty), OrderType.MARKET, Product.CARRY, strategy_id,
                      config_version, client_ref=f"kill-{request_ms}-{strategy_id}-{n}")
        execution.place(order)
        sent.append(order)
        log.warning("ALERT kill switch: %s %d %s", side.value, order.qty, p.instrument)
    return sent


def main():
    import yaml

    from tradeloop.paper_store import PaperStore

    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["kill", "status", "resume"])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--reason", default="manual")
    a = p.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())
    path = Path(cfg.get("kill_file", KILL_FILE))
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
