from datetime import UTC, datetime

import pandas_market_calendars as market_calendars

from trading_system.market.session import NseSessionCalendar, SessionKind


def test_state_at_exposes_utc_bounds_for_a_regular_open_minute():
    state = NseSessionCalendar().state_at(datetime(2026, 8, 3, 4, 15, tzinfo=UTC))

    assert state.kind is SessionKind.OPEN
    assert state.opened_at == datetime(2026, 8, 3, 3, 45, tzinfo=UTC)
    assert state.closed_at == datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def test_state_at_marks_weekends_and_independence_day_weekend_closed():
    calendar = NseSessionCalendar()

    assert calendar.state_at(datetime(2026, 8, 2, 4, 15, tzinfo=UTC)).kind is SessionKind.CLOSED
    assert calendar.state_at(datetime(2026, 8, 15, 4, 15, tzinfo=UTC)).kind is SessionKind.CLOSED


def test_state_at_uses_documented_nse_holiday_calendar_data():
    state = NseSessionCalendar().state_at(datetime(2026, 1, 26, 4, 15, tzinfo=UTC))

    assert state.kind is SessionKind.CLOSED


def test_state_at_blocks_when_the_calendar_horizon_is_unknown():
    state = NseSessionCalendar().state_at(datetime(2028, 1, 3, 4, 15, tzinfo=UTC))

    assert state.kind is SessionKind.BLOCKED
    assert state.reason == "calendar-unknown"


def test_installed_nse_calendar_has_a_2027_regular_session_and_standard_closed_day():
    dependency_calendar = market_calendars.get_calendar("NSE")
    regular_day = dependency_calendar.schedule(start_date="2027-01-01", end_date="2027-01-01")
    closed_day = dependency_calendar.schedule(start_date="2027-01-02", end_date="2027-01-02")
    calendar = NseSessionCalendar()

    assert len(regular_day) == 1
    assert closed_day.empty
    assert calendar.state_at(datetime(2027, 1, 1, 4, 15, tzinfo=UTC)).kind is SessionKind.OPEN
    assert calendar.state_at(datetime(2027, 1, 2, 4, 15, tzinfo=UTC)).kind is SessionKind.CLOSED
