"""Reconciliation (design rule 14): do the ledger and the broker agree on what we hold?

Run at startup, before any trading. A mismatch halts every strategy (no new orders) and
alerts; it does NOT flatten, because we don't know which side is right. Ranga looks, fixes
the cause, and lifts the halt with `python -m tradeloop.kill_switch resume`.

  untagged   the broker holds a position our ledger has no record of (Rule #1: something
             traded outside the bot, or an order lost its tag)
  missing    the ledger holds a position the broker doesn't (closed or assigned elsewhere)
  quantity   both hold it, in different sizes

Contracts past their expiry are skipped: the broker drops them at expiry, while the ledger
keeps them until `paper settle` books the settlement, so the morning after an expiry they
legitimately differ. They are returned separately, so a settlement that never ran is visible.

With only the paper broker (whose positions are the paper ledger carried across restarts)
this mostly guards against bugs. It earns its keep against the real Groww account.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Position
from tradeloop.kill_switch import active, request

log = logging.getLogger("reconcile")


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: str                # untagged | missing | quantity
    instrument: InstrumentId
    ledger_qty: int
    broker_qty: int

    def __str__(self) -> str:
        return f"{self.kind}: {self.instrument} ledger {self.ledger_qty:+d}, broker {self.broker_qty:+d}"


def net(positions: Iterable[Position]) -> dict[InstrumentId, int]:
    out: dict[InstrumentId, int] = defaultdict(int)
    for p in positions:
        out[p.instrument] += p.qty
    return {i: q for i, q in out.items() if q}


def compare(ledger: Iterable[Position], broker: Iterable[Position], today: date) -> tuple[list[Mismatch], list[InstrumentId]]:
    """(mismatches, expired contracts still in the ledger awaiting settlement)."""
    mine, theirs = net(ledger), net(broker)
    awaiting = sorted((i for i in mine if i.expiry is not None and i.expiry < today), key=str)
    live = lambda d: {i: q for i, q in d.items() if i.expiry is None or i.expiry >= today}
    mine, theirs = live(mine), live(theirs)
    out = []
    for i in sorted(mine.keys() | theirs.keys(), key=str):
        a, b = mine.get(i, 0), theirs.get(i, 0)
        if a != b:
            out.append(Mismatch("untagged" if a == 0 else "missing" if b == 0 else "quantity", i, a, b))
    return out, awaiting


def startup_check(traders: list, kill_file: Path, today: date, now_ms: int | None = None) -> list[Mismatch]:
    """Compare every trader's ledger with its broker; on any mismatch halt all strategies."""
    mismatches, awaiting = compare((p for t in traders for p in t.ledger.positions()),
                                   (p for t in traders for p in t.broker.positions()), today)
    for i in awaiting:
        log.warning("ALERT %s expired and still in the ledger: run `python -m tradeloop.paper settle`", i)
    if mismatches:
        for m in mismatches:
            log.error("ALERT reconciliation mismatch, %s", m)
        if active(kill_file) is None:
            request(kill_file, "reconciliation: " + "; ".join(str(m) for m in mismatches), now_ms, flatten=False)
    else:
        live = len(net(p for t in traders for p in t.ledger.positions())) - len(awaiting)
        log.info("Reconciled: ledger and broker agree on %d live positions", live)
    return mismatches
