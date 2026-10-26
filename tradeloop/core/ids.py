"""Neutral instrument IDs (design rule 2).

Every module, table, config and agent output refers to instruments by these
IDs. Broker adapters map them to their own tokens. Format, `|`-separated:

    NSE|NIFTY|IDX                    index
    NSE|RELIANCE|EQ                  equity
    NSE|NIFTY|2026-10-27|FUT         future
    NSE|NIFTY|2026-10-27|25500|CE    option (CE or PE)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class Kind(str, Enum):
    IDX = "IDX"
    EQ = "EQ"
    FUT = "FUT"
    CE = "CE"
    PE = "PE"

    @property
    def is_option(self) -> bool:
        return self in (Kind.CE, Kind.PE)


@dataclass(frozen=True, slots=True)
class InstrumentId:
    exchange: str
    underlying: str
    kind: Kind
    expiry: date | None = None
    strike: float | None = None

    def __post_init__(self):
        if "|" in self.exchange or "|" in self.underlying:
            raise ValueError("exchange and underlying must not contain '|'")
        needs_expiry = self.kind in (Kind.FUT, Kind.CE, Kind.PE)
        if needs_expiry != (self.expiry is not None):
            raise ValueError(f"{self.kind.value} {'needs' if needs_expiry else 'must not have'} an expiry")
        if self.kind.is_option != (self.strike is not None):
            raise ValueError(f"{self.kind.value} {'needs' if self.kind.is_option else 'must not have'} a strike")

    def __str__(self) -> str:
        parts = [self.exchange, self.underlying]
        if self.expiry is not None:
            parts.append(self.expiry.isoformat())
        if self.strike is not None:
            parts.append(_fmt_strike(self.strike))
        parts.append(self.kind.value)
        return "|".join(parts)

    @classmethod
    def parse(cls, text: str) -> InstrumentId:
        parts = text.split("|")
        try:
            kind = Kind(parts[-1])
        except ValueError:
            raise ValueError(f"unknown instrument kind in {text!r}") from None
        expected = {Kind.IDX: 3, Kind.EQ: 3, Kind.FUT: 4, Kind.CE: 5, Kind.PE: 5}[kind]
        if len(parts) != expected:
            raise ValueError(f"{text!r}: {kind.value} IDs have {expected} parts")
        exchange, underlying = parts[0], parts[1]
        expiry = date.fromisoformat(parts[2]) if expected >= 4 else None
        strike = float(parts[3]) if expected == 5 else None
        return cls(exchange, underlying, kind, expiry, strike)

    # Convenience constructors
    @classmethod
    def index(cls, exchange: str, name: str) -> InstrumentId:
        return cls(exchange, name, Kind.IDX)

    @classmethod
    def option(cls, exchange: str, underlying: str, expiry: date, strike: float, kind: str) -> InstrumentId:
        return cls(exchange, underlying, Kind(kind), expiry, float(strike))


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else f"{strike:g}"
