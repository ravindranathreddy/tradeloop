"""Pick which option contracts to record: nearest expiry, strikes around ATM.

Works on neutral Contracts, so it's the same for any broker. Expired contracts
disappear from broker instrument lists, so the recorder also saves the day's
selection to the database (see store.save_contracts).
"""
from collections import Counter
from collections.abc import Iterable
from datetime import date

from tradeloop.core.types import Contract


def options_for(contracts: Iterable[Contract], exchange: str, underlying: str) -> list[Contract]:
    return [c for c in contracts
            if c.instrument.kind.is_option
            and c.instrument.exchange == exchange and c.instrument.underlying == underlying]


def nearest_expiry(options: list[Contract], today: date) -> date | None:
    future = [c.instrument.expiry for c in options if c.instrument.expiry >= today]
    return min(future) if future else None


def strike_step(strikes: Iterable[float]) -> float:
    """The usual gap between strikes (some chains have wider gaps far from ATM)."""
    strikes = sorted(set(strikes))
    gaps = Counter(b - a for a, b in zip(strikes, strikes[1:]))
    return gaps.most_common(1)[0][0] if gaps else 0.0


def select_strikes(chain: list[Contract], spot: float, each_side: int) -> list[Contract]:
    """CE and PE contracts for the ATM strike and `each_side` strikes above and below."""
    step = strike_step(c.instrument.strike for c in chain)
    if step > 0:
        atm = round(spot / step) * step
        lo, hi = atm - each_side * step, atm + each_side * step
        chain = [c for c in chain if lo <= c.instrument.strike <= hi]
    return sorted(chain, key=lambda c: (c.instrument.strike, c.instrument.kind.value))


def pick_options(contracts: Iterable[Contract], exchange: str, underlying: str,
                 spot: float, each_side: int, today: date) -> list[Contract]:
    options = options_for(contracts, exchange, underlying)
    expiry = nearest_expiry(options, today)
    if expiry is None:
        return []
    return select_strikes([c for c in options if c.instrument.expiry == expiry], spot, each_side)
