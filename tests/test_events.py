from collections import Counter
from datetime import date

from tradeloop.core.events import EventCalendar, default_events

CAL = default_events()


def one(d, kind):
    return next(e for e in CAL.events if e.date == d and e.kind == kind)


def test_fomc_decision_reaches_india_the_next_session():
    assert one(date(2025, 10, 29), "fomc").effect == date(2025, 10, 30)       # Wed evening US -> Thu
    assert one(date(2020, 3, 15), "fomc").effect == date(2020, 3, 16)         # emergency cut on a Sunday


def test_rbi_and_budget_react_the_same_session_and_weekend_budgets_use_the_special_session():
    assert one(date(2025, 4, 9), "rbi").effect == date(2025, 4, 9)
    assert one(date(2024, 7, 23), "budget").effect == date(2024, 7, 23)
    assert one(date(2025, 2, 1), "budget").effect == date(2025, 2, 1)         # Saturday, NSE special session
    assert one(date(2024, 6, 4), "election").effect == date(2024, 6, 4)


def test_effect_skips_a_real_holiday_in_a_covered_year():
    # 2026: an FOMC decision whose next day is an Indian holiday reacts on the following session
    for e in CAL.events:
        if e.date.year == 2026 and e.kind == "fomc":
            assert e.effect > e.date


def test_between_is_inclusive_on_reaction_day_and_filters_kinds():
    got = CAL.between(date(2025, 10, 29), date(2025, 10, 30))
    assert [(e.kind, e.effect) for e in got] == [("fomc", date(2025, 10, 30))]
    assert CAL.between(date(2025, 10, 29), date(2025, 10, 30), {"rbi"}) == []


def test_coverage_looks_complete():
    fomc = Counter(e.date.year for e in CAL.events if e.kind == "fomc")
    assert all(fomc[y] == 8 for y in (2021, 2022, 2023, 2024, 2025, 2026)) and fomc[2020] == 9
    rbi = Counter(e.date.year for e in CAL.events if e.kind == "rbi")
    assert all(rbi[y] >= 6 for y in range(2020, 2027))
    assert Counter(e.date.year for e in CAL.events if e.kind == "budget")[2024] == 2     # interim + full


def test_no_duplicates_and_bad_files_fail_loudly(tmp_path):
    keys = [(e.date, e.kind) for e in CAL.events]
    assert len(keys) == len(set(keys))
    bad = tmp_path / "e.yaml"
    bad.write_text("events:\n  - {date: 2026-01-01, kind: rbi, name: x, when: sometime, source: news}\n")
    try:
        EventCalendar.load(bad)
    except ValueError as e:
        assert "when must be" in str(e)
    else:
        raise AssertionError("bad 'when' accepted")


def test_a_holiday_before_2026_is_set_by_hand():
    assert one(date(2021, 11, 3), "fomc").effect == date(2021, 11, 8)           # Diwali, 4-5 Nov
