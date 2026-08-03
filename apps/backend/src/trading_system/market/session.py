"""NSE trading-session decisions backed only by pandas-market-calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

import pandas_market_calendars as market_calendars  # type: ignore[import-untyped]


class SessionKind(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SessionState:
    """The exchange state and, when known, UTC bounds of its current session."""

    kind: SessionKind
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("opened_at", "closed_at"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
            object.__setattr__(self, field_name, value.astimezone(UTC))


class NseSessionCalendar:
    """A deliberately bounded wrapper around the dependency's NSE calendar."""

    _coverage_start = date(2026, 1, 1)
    _coverage_end = date(2027, 12, 31)
    _timezone = ZoneInfo("Asia/Kolkata")

    def __init__(self) -> None:
        self._calendar = market_calendars.get_calendar("NSE")

    def state_at(self, instant: datetime) -> SessionState:
        """Return the session state for an aware instant without guessing outside coverage."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("instant must be timezone-aware")
        utc_instant = instant.astimezone(UTC)
        exchange_date = utc_instant.astimezone(self._timezone).date()
        if not self._coverage_start <= exchange_date <= self._coverage_end:
            return SessionState(kind=SessionKind.BLOCKED, reason="calendar-unknown")

        schedule = self._calendar.schedule(start_date=exchange_date, end_date=exchange_date)
        if schedule.empty:
            return SessionState(kind=SessionKind.CLOSED, reason="exchange-closed")

        row = schedule.iloc[0]
        opened_at = row["market_open"].to_pydatetime().astimezone(UTC)
        closed_at = row["market_close"].to_pydatetime().astimezone(UTC)
        kind = SessionKind.OPEN if opened_at <= utc_instant < closed_at else SessionKind.CLOSED
        reason = None if kind is SessionKind.OPEN else "outside-session"
        return SessionState(kind=kind, opened_at=opened_at, closed_at=closed_at, reason=reason)
