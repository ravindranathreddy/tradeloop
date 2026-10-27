"""Market calendar (design rule 15): trading days, session times, expiry days.

Facts live in market_calendar.yaml at the repo root. Everything that needs to
know "is the market open" or "when does NIFTY expire" asks this module rather
than working dates out itself.

Asking about a year with no holiday list raises UnknownYear: silently treating
every weekday as a trading day is worse than a loud error.
"""
from __future__ import annotations

import functools
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from tradeloop.core.ids import InstrumentId

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "market_calendar.yaml"
WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4}


class UnknownYear(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class Session:
    pre_open: datetime
    open: datetime
    close: datetime


@dataclass(frozen=True, slots=True)
class ExpiryRule:
    start: date
    weekday: int


class MarketCalendar:
    def __init__(self, holidays: dict[int, dict[date, str]], session_times: dict[str, time],
                 expiry_rules: dict[InstrumentId, list[ExpiryRule]],
                 special_sessions: dict[date, str] | None = None,
                 session_changes: dict[date, dict[str, time]] | None = None):
        self.holidays = holidays
        self.session_times = session_times
        self.session_changes = sorted((session_changes or {}).items())
        self.expiry_rules = {k: sorted(v, key=lambda r: r.start) for k, v in expiry_rules.items()}
        self.special_sessions = special_sessions or {}

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> MarketCalendar:
        with open(path) as f:
            raw = yaml.safe_load(f)
        holidays = {int(y): {_date(d): str(name) for d, name in days.items()}
                    for y, days in (raw.get("holidays") or {}).items()}
        times = _times(raw["session"])
        changes = {_date(d): _times(t) for d, t in (raw.get("session_changes") or {}).items()}
        rules = {InstrumentId.parse(k): [ExpiryRule(_date(r["from"]), WEEKDAYS[r["weekday"]]) for r in v]
                 for k, v in (raw.get("expiry_rules") or {}).items()}
        special = {_date(d): str(n) for d, n in (raw.get("special_sessions") or {}).items()}
        return cls(holidays, times, rules, special, changes)

    # --- trading days ----------------------------------------------------------

    def covers(self, year: int) -> bool:
        return year in self.holidays

    def holiday(self, d: date) -> str | None:
        """The holiday's name if d is a weekday holiday, else None."""
        if not self.covers(d.year):
            raise UnknownYear(f"no holiday list for {d.year} in market_calendar.yaml")
        return self.holidays[d.year].get(d)

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:
            if not self.covers(d.year):     # still fail loudly on unknown years
                raise UnknownYear(f"no holiday list for {d.year} in market_calendar.yaml")
            return False
        return self.holiday(d) is None

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def previous_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def trading_days(self, start: date, end: date) -> list[date]:
        """Trading days from start to end, both inclusive."""
        days, d = [], start
        while d <= end:
            if self.is_trading_day(d):
                days.append(d)
            d += timedelta(days=1)
        return days

    # --- sessions ----------------------------------------------------------------

    def session_times_on(self, d: date) -> dict[str, time]:
        """Session times in force on `d`. Needs no holiday list, so works for any year."""
        times = dict(self.session_times)
        for start, changed in self.session_changes:
            if start <= d:
                times.update(changed)
        return times

    def close_time(self, d: date) -> time:
        return self.session_times_on(d)["close"]

    def session(self, d: date) -> Session | None:
        if not self.is_trading_day(d):
            return None
        times = self.session_times_on(d)
        at = lambda key: datetime.combine(d, times[key], IST)
        return Session(at("pre_open"), at("open"), at("close"))

    def is_open(self, when: datetime) -> bool:
        """True during the normal market (open to close), not pre-open."""
        when = when.astimezone(IST)
        s = self.session(when.date())
        return s is not None and s.open <= when < s.close

    # --- expiries ----------------------------------------------------------------

    def _rule(self, index: InstrumentId, scheduled: date) -> ExpiryRule | None:
        found = None
        for r in self.expiry_rules.get(index, []):
            if r.start <= scheduled:
                found = r
        return found

    def next_expiry(self, index: InstrumentId, d: date) -> date:
        """The first weekly expiry on or after d, moved earlier if it falls on a holiday."""
        rules = self.expiry_rules.get(index)
        if not rules:
            raise KeyError(f"no expiry rule for {index}")
        if d < rules[0].start:
            raise ValueError(f"{index}: no expiry rule before {rules[0].start} (use broker data)")
        s = d
        while True:
            rule = self._rule(index, s)
            if s.weekday() == rule.weekday:
                actual = s if self.is_trading_day(s) else self.previous_trading_day(s)
                if actual >= d:
                    return actual
            s += timedelta(days=1)

    def expiries(self, index: InstrumentId, start: date, end: date) -> list[date]:
        """Weekly expiries from start to end, both inclusive. Looks no further than `end`,
        so it never needs next year's holiday list."""
        out, s = [], start
        while s <= end:
            rule = self._rule(index, s)
            if rule is not None and s.weekday() == rule.weekday:
                actual = s if self.is_trading_day(s) else self.previous_trading_day(s)
                if actual >= start:
                    out.append(actual)
            s += timedelta(days=1)
        return out

    def is_expiry_day(self, index: InstrumentId, d: date) -> bool:
        return self.is_trading_day(d) and self.next_expiry(index, d) == d

    def is_monthly_expiry(self, index: InstrumentId, d: date) -> bool:
        """The last weekly expiry of its month."""
        if not self.is_expiry_day(index, d):
            return False
        month_end = (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return not self.expiries(index, d + timedelta(days=1), month_end)

    def check_expiries(self, index: InstrumentId, listed: Iterable[date]) -> list[str]:
        """Compare broker-listed expiries with the calendar's. Returns problems found.

        Broker lists only some weeks ahead, so a missing broker date isn't a problem;
        a broker date the calendar wouldn't produce is (usually a wrong holiday list).
        Dates outside the rules or the covered years are skipped.
        """
        rules = self.expiry_rules.get(index)
        if not rules:
            return [f"{index}: no expiry rule in market_calendar.yaml"]
        problems = []
        for d in sorted(set(listed)):
            if d < rules[0].start or not self.covers(d.year):
                continue
            if not self.is_trading_day(d):
                problems.append(f"{index}: broker expiry {d} is not a trading day in the calendar")
            elif not self.is_expiry_day(index, d):
                problems.append(f"{index}: broker expiry {d}, calendar says {self.next_expiry(index, d)}")
        return problems


def _date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _times(raw: dict) -> dict[str, time]:
    return {k: datetime.strptime(v, "%H:%M").time() for k, v in raw.items()}


@functools.cache
def default_calendar() -> MarketCalendar:
    """The repo's market_calendar.yaml, loaded once."""
    return MarketCalendar.load()
