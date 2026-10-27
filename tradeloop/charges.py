"""Charges on index options and futures, at the rates in force on the trade date
(design rule 9). Rates and their sources live in charges.yaml.

Money is integer paise. Each charge is computed exactly (Decimal) and rounded half-up
per fill: STT to the rupee (as Groww does), the rest to the paisa. Brokerage is per
order, so the caller says whether this fill is the order's first.
"""
from __future__ import annotations

import functools
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.types import Side

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "charges.yaml"
CHARGES = ("brokerage", "stt", "exchange", "sebi", "ipft", "stamp", "gst")
_HUNDRED = Decimal(100)


class ChargeSchedule:
    def __init__(self, raw: dict):
        self._raw = raw

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> ChargeSchedule:
        with open(path) as f:
            return cls(yaml.safe_load(f))

    def _rate(self, d: date, charge: str, field: str, exchange: str | None = None) -> Decimal:
        entries = self._raw[charge] if exchange is None else self._raw[charge][exchange]
        current = [e for e in entries if _date(e["from"]) <= d]
        if not current:
            raise KeyError(f"no {charge} rate on {d}")
        return Decimal(str(max(current, key=lambda e: _date(e["from"]))[field]))

    def trade(self, d: date, instrument: InstrumentId, side: Side, value_paise: int,
              first_fill_of_order: bool = True) -> dict[str, int]:
        """Charges in paise for one fill worth `value_paise` (price x quantity)."""
        if instrument.kind not in (Kind.CE, Kind.PE, Kind.FUT):
            raise ValueError(f"charges are for futures and options, not {instrument}")
        kind = "option" if instrument.kind.is_option else "future"
        ex = instrument.exchange
        value = Decimal(value_paise) / _HUNDRED                  # rupees
        pct = lambda rate: value * rate / _HUNDRED
        c = {
            "brokerage": self._rate(d, "brokerage", "per_order") if first_fill_of_order else Decimal(0),
            "stt": pct(self._rate(d, "stt", f"{kind}_sell")) if side is Side.SELL else Decimal(0),
            "exchange": pct(self._rate(d, "exchange", kind, ex)),
            "sebi": pct(self._rate(d, "sebi", "rate")),
            "ipft": pct(self._rate(d, "ipft", kind, ex)),
            "stamp": pct(self._rate(d, "stamp", f"{kind}_buy")) if side is Side.BUY else Decimal(0),
        }
        out = {k: _paise(v) for k, v in c.items()}
        out["stt"] = _rupee(c["stt"])
        taxable = out["brokerage"] + out["exchange"] + out["sebi"] + out["ipft"]
        out["gst"] = _paise(Decimal(taxable) / _HUNDRED * self._rate(d, "gst", "rate") / _HUNDRED)
        return out

    def exercise(self, d: date, intrinsic_value_paise: int) -> dict[str, int]:
        """STT on long in-the-money options settled at expiry (on intrinsic value)."""
        rate = self._rate(d, "stt", "option_exercise")
        return {"stt": _rupee(Decimal(intrinsic_value_paise) / _HUNDRED * rate / _HUNDRED)}


def _paise(rupees: Decimal) -> int:
    return int((rupees * _HUNDRED).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _rupee(rupees: Decimal) -> int:
    return int(rupees.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 100


def _date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


@functools.cache
def default_charges() -> ChargeSchedule:
    return ChargeSchedule.load()
