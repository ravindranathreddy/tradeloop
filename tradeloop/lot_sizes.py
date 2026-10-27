"""Lot size of an index derivative on any date since 2020 (facts in lot_sizes.yaml).

Groww's history API doesn't give lot sizes, and they have changed four times for NIFTY
since 2020, so backtests look them up here by the contract's expiry.
"""
from __future__ import annotations

import functools
from datetime import date
from pathlib import Path

import yaml

from tradeloop.core.ids import InstrumentId

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "lot_sizes.yaml"


class LotSizes:
    def __init__(self, table: dict[InstrumentId, tuple[list[tuple[date, int]], dict[date, int]]]):
        self._table = table

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> LotSizes:
        with open(path) as f:
            raw = yaml.safe_load(f)
        table = {}
        for index, spec in raw.items():
            changes = sorted((_date(c["from"]), int(c["lot"])) for c in spec["changes"])
            exceptions = {_date(d): int(lot) for d, lot in (spec.get("exceptions") or {}).items()}
            table[InstrumentId.parse(index)] = (changes, exceptions)
        return cls(table)

    def lot_size(self, instrument: InstrumentId) -> int:
        """For a future or option; raises KeyError for an unknown underlying or a date before the table."""
        index = InstrumentId.index(instrument.exchange, instrument.underlying)
        changes, exceptions = self._table[index]
        expiry = instrument.expiry
        if expiry in exceptions:
            return exceptions[expiry]
        lots = [lot for start, lot in changes if start <= expiry]
        if not lots:
            raise KeyError(f"no lot size for {instrument} (table starts {changes[0][0]})")
        return lots[-1]


def _date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


@functools.cache
def default_lot_sizes() -> LotSizes:
    return LotSizes.load()
