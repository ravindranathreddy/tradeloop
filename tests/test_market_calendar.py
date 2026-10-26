from datetime import date, datetime, time, timezone

import pytest

from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST, ExpiryRule, MarketCalendar, UnknownYear
from tradeloop.core.types import Contract
from tradeloop.recorder import calendar_problems

NIFTY = InstrumentId.index("NSE", "NIFTY")
SENSEX = InstrumentId.index("BSE", "SENSEX")

# Expiries listed in Groww's instrument CSV on 2026-10-26 (2026 dates only).
GROWW_NIFTY = [date(2026, 10, 27), date(2026, 11, 3), date(2026, 11, 9), date(2026, 11, 17),
               date(2026, 11, 23), date(2026, 12, 1), date(2026, 12, 29)]
GROWW_SENSEX = [date(2026, 10, 29), date(2026, 11, 5), date(2026, 11, 12), date(2026, 11, 19),
                date(2026, 11, 26), date(2026, 12, 3), date(2026, 12, 10), date(2026, 12, 31)]


@pytest.fixture(scope="module")
def cal():
    return MarketCalendar.load()          # the real market_calendar.yaml


def d(s):
    return date.fromisoformat(s)


# --- trading days -------------------------------------------------------------

def test_weekends_and_holidays(cal):
    assert cal.is_trading_day(d("2026-10-26"))              # Monday
    assert not cal.is_trading_day(d("2026-10-24"))          # Saturday
    assert not cal.is_trading_day(d("2026-11-10"))          # Diwali-Balipratipada
    assert cal.holiday(d("2026-11-24")) == "Prakash Gurpurb Sri Guru Nanak Dev"
    assert not cal.is_trading_day(d("2026-11-08"))          # Muhurat Sunday: not a regular day


def test_unknown_year_fails_loudly(cal):
    with pytest.raises(UnknownYear):
        cal.is_trading_day(d("2027-01-04"))
    with pytest.raises(UnknownYear):
        cal.is_trading_day(d("2027-01-02"))                 # even a Saturday


def test_next_and_previous_trading_day(cal):
    assert cal.next_trading_day(d("2026-11-06")) == d("2026-11-09")       # Fri -> Mon
    assert cal.next_trading_day(d("2026-11-09")) == d("2026-11-11")       # skips Tue holiday
    assert cal.previous_trading_day(d("2026-10-21")) == d("2026-10-19")   # skips Dussehra
    assert len(cal.trading_days(d("2026-11-01"), d("2026-11-30"))) == 19


def test_session_times(cal):
    s = cal.session(d("2026-10-27"))
    assert (s.pre_open.time(), s.open.time(), s.close.time()) == (time(9, 0), time(9, 15), time(15, 30))
    assert s.open.tzinfo == IST
    assert cal.session(d("2026-11-10")) is None
    assert cal.is_open(datetime(2026, 10, 27, 9, 15, tzinfo=IST))
    assert not cal.is_open(datetime(2026, 10, 27, 9, 10, tzinfo=IST))   # pre-open
    assert not cal.is_open(datetime(2026, 10, 27, 15, 30, tzinfo=IST))
    assert cal.is_open(datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc))         # 09:30 IST


# --- expiries -------------------------------------------------------------------

def test_holiday_expiry_moves_to_previous_trading_day(cal):
    assert cal.next_expiry(NIFTY, d("2026-11-04")) == d("2026-11-09")     # Tue 10 Nov is a holiday
    assert cal.next_expiry(NIFTY, d("2026-11-10")) == d("2026-11-17")     # 9 Nov already passed
    assert cal.next_expiry(SENSEX, d("2026-10-27")) == d("2026-10-29")


def test_calendar_matches_groww_listed_expiries(cal):
    assert set(GROWW_NIFTY) <= set(cal.expiries(NIFTY, d("2026-10-26"), d("2026-12-31")))
    assert set(GROWW_SENSEX) <= set(cal.expiries(SENSEX, d("2026-10-26"), d("2026-12-31")))
    assert cal.check_expiries(NIFTY, GROWW_NIFTY) == []
    assert cal.check_expiries(SENSEX, GROWW_SENSEX) == []


def test_monthly_expiry(cal):
    assert cal.is_monthly_expiry(NIFTY, d("2026-11-23"))                  # last Tue (24th) is a holiday
    assert not cal.is_monthly_expiry(NIFTY, d("2026-11-17"))
    assert cal.is_monthly_expiry(NIFTY, d("2026-12-29"))
    assert not cal.is_expiry_day(NIFTY, d("2026-11-24"))


def test_missing_holiday_is_caught_by_the_broker_check(cal):
    no_balipratipada = {y: {k: v for k, v in days.items() if k != d("2026-11-10")}
                        for y, days in cal.holidays.items()}
    wrong = MarketCalendar(no_balipratipada, cal.session_times, cal.expiry_rules)
    assert wrong.check_expiries(NIFTY, GROWW_NIFTY) == [
        "NSE|NIFTY|IDX: broker expiry 2026-11-09, calendar says 2026-11-10"]


def test_dates_before_the_rules_are_left_to_broker_data(cal):
    with pytest.raises(ValueError):
        cal.next_expiry(NIFTY, d("2025-06-01"))
    assert cal.check_expiries(NIFTY, [d("2025-06-26")]) == []             # skipped, not flagged


def test_rule_change_takes_effect_on_its_date():
    holidays = {2026: {}}
    rules = {NIFTY: [ExpiryRule(d("2026-01-01"), 3), ExpiryRule(d("2026-06-01"), 1)]}  # Thu, then Tue
    cal = MarketCalendar(holidays, {"pre_open": time(9), "open": time(9, 15), "close": time(15, 30)}, rules)
    assert cal.expiries(NIFTY, d("2026-05-25"), d("2026-06-10")) == [d("2026-05-28"), d("2026-06-02"), d("2026-06-09")]


# --- recorder's startup check --------------------------------------------------------

def test_recorder_calendar_check(cal):
    contracts = [Contract(InstrumentId.option("NSE", "NIFTY", e, 25500, "CE"), 65) for e in GROWW_NIFTY]
    contracts.append(Contract(InstrumentId.option("NSE", "NIFTY", d("2026-11-10"), 25500, "CE"), 65))
    underlyings = [{"name": "NIFTY", "exchange": "NSE"}]
    assert calendar_problems(cal, contracts, underlyings) == [
        "NSE|NIFTY|IDX: broker expiry 2026-11-10 is not a trading day in the calendar"]
