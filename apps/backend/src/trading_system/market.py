import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import (
    Candle,
    GlobalState,
    HealthState,
    Instrument,
    MarketObservation,
    NiftyUniverse,
)

IST = ZoneInfo("Asia/Kolkata")


class SessionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class SessionCalendar:
    valid_from: date
    valid_through: date
    regular_open: time
    regular_close: time
    weekend_days: frozenset[int]
    holidays: frozenset[date]
    exceptional_sessions_pending: frozenset[date]


@dataclass(frozen=True)
class Session:
    state: SessionState
    reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state is SessionState.OPEN


def load_session_calendar(path: str | Path) -> SessionCalendar:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("timezone") != "Asia/Kolkata":
        raise ValueError("calendar timezone must be Asia/Kolkata")
    return SessionCalendar(
        valid_from=date.fromisoformat(payload["valid_from"]),
        valid_through=date.fromisoformat(payload["valid_through"]),
        regular_open=time.fromisoformat(payload["regular_open"]),
        regular_close=time.fromisoformat(payload["regular_close"]),
        weekend_days=frozenset(payload["weekend_days"]),
        holidays=frozenset(map(date.fromisoformat, payload["holidays"])),
        exceptional_sessions_pending=frozenset(
            map(date.fromisoformat, payload.get("exceptional_sessions_pending", ()))
        ),
    )


def session_at(calendar: SessionCalendar, at: datetime) -> Session:
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    local = at.astimezone(IST)
    day = local.date()
    if not calendar.valid_from <= day <= calendar.valid_through:
        return Session(SessionState.BLOCKED, "CALENDAR_UNKNOWN")
    if day in calendar.exceptional_sessions_pending:
        return Session(SessionState.BLOCKED, "EXCEPTIONAL_SESSION_PENDING")
    if day.weekday() in calendar.weekend_days or day in calendar.holidays:
        return Session(SessionState.CLOSED, "MARKET_CLOSED")
    if calendar.regular_open <= local.time().replace(tzinfo=None) < calendar.regular_close:
        return Session(SessionState.OPEN)
    return Session(SessionState.CLOSED, "MARKET_CLOSED")


def global_state(states: list[HealthState] | tuple[HealthState, ...], session: Session) -> GlobalState:
    if session.state is SessionState.BLOCKED or HealthState.BLOCKED in states:
        return GlobalState.BLOCKED
    if session.is_open and HealthState.STALE in states:
        return GlobalState.STALE
    if HealthState.DEGRADED in states:
        return GlobalState.DEGRADED
    if not session.is_open:
        return GlobalState.MARKET_CLOSED
    return GlobalState.LIVE


def freshness_state(last_success: datetime | None, now: datetime, threshold_seconds: int) -> HealthState:
    if last_success is None:
        return HealthState.BLOCKED
    if last_success.tzinfo is None or now.tzinfo is None:
        raise ValueError("freshness timestamps must be timezone-aware")
    return (
        HealthState.STALE
        if now.astimezone(timezone.utc) - last_success.astimezone(timezone.utc)
        > timedelta(seconds=threshold_seconds)
        else HealthState.HEALTHY
    )


def resolve_nifty_universe(
    instruments: list[Instrument] | tuple[Instrument, ...],
    spot_price: Decimal,
    as_of: date,
    window: int = 5,
) -> NiftyUniverse:
    nifty = [
        item
        for item in instruments
        if (item.underlying_symbol or item.trading_symbol).upper().replace(" ", "")
        in {"NIFTY", "NIFTY50"}
    ]
    spots = [item for item in nifty if item.instrument_type.upper() in {"IDX", "INDEX"} and item.expiry is None]
    futures = [
        item
        for item in nifty
        if "FUT" in item.instrument_type.upper() and item.expiry is not None and item.expiry >= as_of
    ]
    options = [
        item
        for item in nifty
        if item.option_type is not None
        and item.strike is not None
        and item.expiry is not None
        and item.expiry >= as_of
    ]
    if not spots or not futures or not options:
        raise ValueError("instrument master does not contain the required NIFTY contracts")

    future = min(futures, key=lambda item: (item.expiry, item.trading_symbol))
    option_expiry = min(item.expiry for item in options)
    expiry_options = [item for item in options if item.expiry == option_expiry]
    strikes = sorted({item.strike for item in expiry_options if item.strike is not None})
    atm = min(strikes, key=lambda strike: (abs(strike - spot_price), strike))
    atm_index = strikes.index(atm)
    selected_strikes = set(strikes[max(0, atm_index - window) : atm_index + window + 1])
    selected = tuple(
        sorted(
            (item for item in expiry_options if item.strike in selected_strikes),
            key=lambda item: (item.strike or Decimal(0), item.option_type or "", item.trading_symbol),
        )
    )
    return NiftyUniverse(
        spot=sorted(spots, key=lambda item: item.trading_symbol)[0],
        future=future,
        option_expiry=option_expiry,
        atm_strike=atm,
        options=selected,
    )


def minute_bucket(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("event timestamp must be timezone-aware")
    return at.astimezone(timezone.utc).replace(second=0, microsecond=0)


class CandleBuilder:
    def __init__(self) -> None:
        self._pending: dict[tuple[str, datetime], list[MarketObservation]] = defaultdict(list)
        self._seen: set[str] = set()
        self._finalized: set[tuple[str, datetime]] = set()
        self._late: list[MarketObservation] = []

    @property
    def late_observations(self) -> tuple[MarketObservation, ...]:
        return tuple(self._late)

    def add(self, observation: MarketObservation) -> str:
        if observation.observation_id in self._seen:
            return "DUPLICATE"
        self._seen.add(observation.observation_id)
        key = (observation.instrument, minute_bucket(observation.event_at))
        if key in self._finalized:
            self._late.append(observation)
            return "LATE"
        self._pending[key].append(observation)
        return "ACCEPTED"

    def finalize_before(self, cutoff: datetime) -> tuple[Candle, ...]:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        cutoff = cutoff.astimezone(timezone.utc)
        ready = sorted(
            (key for key in self._pending if key[1] + timedelta(minutes=1) <= cutoff),
            key=lambda key: (key[1], key[0]),
        )
        candles = []
        for key in ready:
            observations = sorted(
                self._pending.pop(key),
                key=lambda item: (item.event_at, item.received_at, item.observation_id),
            )
            prices = [item.price for item in observations]
            volumes = [item.cumulative_volume for item in observations]
            volume = None
            if len(volumes) > 1 and all(value is not None for value in volumes):
                typed_volumes = [value for value in volumes if value is not None]
                if typed_volumes == sorted(typed_volumes):
                    volume = typed_volumes[-1] - typed_volumes[0]
            candles.append(
                Candle(
                    instrument=key[0],
                    interval="1m",
                    bucket_start=key[1],
                    open=prices[0],
                    high=max(prices),
                    low=min(prices),
                    close=prices[-1],
                    volume=volume,
                    observation_ids=tuple(item.observation_id for item in observations),
                )
            )
            self._finalized.add(key)
        return tuple(candles)


def aggregate_five_minute(candles: list[Candle] | tuple[Candle, ...]) -> tuple[Candle, ...]:
    groups: dict[tuple[str, datetime], list[Candle]] = defaultdict(list)
    for candle in candles:
        if candle.interval != "1m" or not candle.finalized:
            continue
        bucket = candle.bucket_start.astimezone(timezone.utc).replace(
            minute=candle.bucket_start.minute - candle.bucket_start.minute % 5,
            second=0,
            microsecond=0,
        )
        groups[(candle.instrument, bucket)].append(candle)

    result = []
    for (instrument, bucket), group in sorted(groups.items(), key=lambda item: item[0][1]):
        ordered = sorted(group, key=lambda candle: candle.bucket_start)
        expected = [bucket + timedelta(minutes=offset) for offset in range(5)]
        if [candle.bucket_start.astimezone(timezone.utc) for candle in ordered] != expected:
            continue
        result.append(
            Candle(
                instrument=instrument,
                interval="5m",
                bucket_start=bucket,
                open=ordered[0].open,
                high=max(candle.high for candle in ordered),
                low=min(candle.low for candle in ordered),
                close=ordered[-1].close,
                volume=(
                    sum((candle.volume for candle in ordered), Decimal(0))
                    if all(candle.volume is not None for candle in ordered)
                    else None
                ),
                observation_ids=tuple(
                    observation_id for candle in ordered for observation_id in candle.observation_ids
                ),
            )
        )
    return tuple(result)
