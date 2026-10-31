"""Scheduled events that can move the index (RBI policy, FOMC, Union Budget, election results).

Facts live in events.yaml at the repo root, each with the first Indian session that reacts to
it (`effect`) and how well its date was checked (`source`). Used by research now and, if a
rule passes its tests, by an entry filter later. Skips nothing by itself.

Holidays: for years the market calendar covers (2026 on) the effect day is a real trading day;
for earlier years it is the next weekday, which can be a holiday in rare cases.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from tradeloop.core.market_calendar import UnknownYear, default_calendar

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "events.yaml"
WHEN = ("in_session", "after_close")


@dataclass(frozen=True, slots=True)
class Event:
    date: date
    kind: str            # rbi | fomc | budget | election
    name: str
    effect: date         # first Indian session that can react
    source: str          # official | news | recalled
    when: str = "in_session"   # in_session | after_close (in India)


def _open(d: date) -> bool:
    try:
        return default_calendar().is_trading_day(d)
    except UnknownYear:
        return d.weekday() < 5


def _next_open(d: date) -> date:
    d += timedelta(days=1)
    while not _open(d):
        d += timedelta(days=1)
    return d


class EventCalendar:
    def __init__(self, events: list[Event]):
        self.events = sorted(events, key=lambda e: (e.effect, e.kind))

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> EventCalendar:
        out = []
        for raw in yaml.safe_load(Path(path).read_text())["events"]:
            when = raw["when"]
            if when not in WHEN:
                raise ValueError(f"{raw['date']}: when must be one of {WHEN}")
            d = raw["date"]
            effect = raw.get("effect") or (d if when == "in_session" and _open(d) else _next_open(d))
            out.append(Event(d, raw["kind"], raw["name"], effect, raw["source"], when))
        return cls(out)

    def between(self, start: date, end: date, kinds: set[str] | None = None) -> list[Event]:
        """Events whose reaction day falls in [start, end], both inclusive."""
        return [e for e in self.events if start <= e.effect <= end and (kinds is None or e.kind in kinds)]

    def unchecked(self) -> list[Event]:
        return [e for e in self.events if e.source == "recalled"]


@functools.cache
def default_events() -> EventCalendar:
    return EventCalendar.load()
