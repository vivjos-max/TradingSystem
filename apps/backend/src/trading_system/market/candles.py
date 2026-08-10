"""Event-time, session-bounded construction of finalized market candles."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_system.domain.models import Candle
from trading_system.market.session import NseSessionCalendar, SessionKind


@dataclass(frozen=True, slots=True)
class CandleObservation:
    instrument_token: str
    observed_at: datetime
    price: Decimal
    volume: int | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class CandleUpdate:
    candle: Candle | None = None
    late: bool = False
    late_observation: CandleObservation | None = None


class CandleBuilder:
    """Collect observations by event-time minute; finalized minutes are immutable.

    Identical event timestamps preserve acceptance order: first accepted sets open and last
    accepted sets close.
    """

    def __init__(self, calendar: NseSessionCalendar) -> None:
        self._calendar = calendar
        self._observations: dict[tuple[str, datetime], list[CandleObservation]] = defaultdict(list)
        self._finalized: dict[tuple[str, datetime], Candle] = {}

    def accept(self, observation: CandleObservation) -> CandleUpdate:
        minute = observation.observed_at.replace(second=0, microsecond=0)
        key = (observation.instrument_token, minute)
        if key in self._finalized:
            return CandleUpdate(
                candle=self._finalized[key], late=True, late_observation=observation
            )
        session = self._calendar.state_at(observation.observed_at)
        if session.kind is not SessionKind.OPEN:
            raise ValueError("observation is outside an open NSE session")
        self._observations[key].append(observation)
        return CandleUpdate()

    def finalize_through(self, instant: datetime) -> tuple[Candle, ...]:
        """Finalize complete half-open minutes at or before an injected event-time cutoff."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("instant must be timezone-aware")
        cutoff = instant.astimezone(UTC)
        completed_keys = sorted(
            key for key in self._observations if key[1] + timedelta(minutes=1) <= cutoff
        )
        finalized: list[Candle] = []
        for key in completed_keys:
            observations = self._observations.pop(key)
            token, opened_at = key
            session = self._calendar.state_at(opened_at)
            if session.kind is not SessionKind.OPEN:
                continue
            ordered = sorted(observations, key=lambda item: item.observed_at)
            volumes = [item.volume for item in ordered]
            candle = Candle(
                instrument_token=token,
                opened_at=opened_at,
                closed_at=opened_at + timedelta(minutes=1),
                open=ordered[0].price,
                high=max(item.price for item in ordered),
                low=min(item.price for item in ordered),
                close=ordered[-1].price,
                volume=(
                    None
                    if any(volume is None for volume in volumes)
                    else sum(volume for volume in volumes if volume is not None)
                ),
                session_date=opened_at.astimezone(self._calendar._timezone).date(),
            )
            self._finalized[key] = candle
            finalized.append(candle)
        return tuple(finalized)


def aggregate_five_minute(candles: tuple[Candle, ...] | list[Candle]) -> Candle:
    """Aggregate precisely five contiguous, finalized one-minute session candles."""
    ordered = tuple(sorted(candles, key=lambda candle: candle.opened_at))
    if len(ordered) != 5:
        raise ValueError("five-minute aggregation requires exactly five candles")
    first = ordered[0]
    if any(
        candle.instrument_token != first.instrument_token
        or candle.session_date != first.session_date
        or not candle.finalized
        or candle.closed_at - candle.opened_at != timedelta(minutes=1)
        for candle in ordered
    ):
        raise ValueError("candles must be finalized one-minute candles from one exchange session")
    if any(
        current.opened_at != previous.closed_at
        for previous, current in zip(ordered, ordered[1:])
    ):
        raise ValueError("candles must be contiguous")
    volumes = [candle.volume for candle in ordered]
    return Candle(
        instrument_token=first.instrument_token,
        opened_at=first.opened_at,
        closed_at=ordered[-1].closed_at,
        open=first.open,
        high=max(candle.high for candle in ordered),
        low=min(candle.low for candle in ordered),
        close=ordered[-1].close,
        volume=(
            None
            if any(volume is None for volume in volumes)
            else sum(volume for volume in volumes if volume is not None)
        ),
        session_date=first.session_date,
    )
