from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_system.market import (
    CandleBuilder,
    Session,
    SessionState,
    aggregate_five_minute,
    global_state,
    load_session_calendar,
    resolve_nifty_universe,
    session_at,
)
from trading_system.models import Candle, GlobalState, HealthState, Instrument, MarketObservation

CALENDAR = Path(__file__).parents[2] / "config" / "nse_sessions_2026.json"
UTC = timezone.utc


def instrument(symbol: str, kind: str, **values: object) -> Instrument:
    return Instrument(
        trading_symbol=symbol,
        exchange_token=symbol,
        exchange="NSE",
        segment="FNO" if kind != "INDEX" else "CASH",
        instrument_type=kind,
        underlying_symbol="NIFTY",
        **values,
    )


def test_calendar_unknown_blocks_and_state_precedence_is_honest() -> None:
    calendar = load_session_calendar(CALENDAR)
    assert session_at(calendar, datetime(2027, 1, 4, 10, tzinfo=UTC)).state is SessionState.BLOCKED
    assert session_at(calendar, datetime(2026, 1, 26, 5, tzinfo=UTC)).state is SessionState.CLOSED

    open_session = Session(SessionState.OPEN)
    closed_session = Session(SessionState.CLOSED)
    assert (
        global_state([HealthState.BLOCKED, HealthState.STALE], open_session) is GlobalState.BLOCKED
    )
    assert (
        global_state([HealthState.STALE, HealthState.DEGRADED], open_session) is GlobalState.STALE
    )
    assert global_state([HealthState.DEGRADED], closed_session) is GlobalState.DEGRADED
    assert global_state([HealthState.HEALTHY], closed_session) is GlobalState.MARKET_CLOSED
    assert global_state([HealthState.HEALTHY], open_session) is GlobalState.LIVE


def test_universe_uses_master_expiries_strikes_and_contract_metadata() -> None:
    near = date(2026, 8, 13)
    later = date(2026, 8, 20)
    strikes = [Decimal(value) for value in (22000, 22040, 22090, 22150, 22220, 22300, 22390)]
    instruments = [
        instrument("NIFTY", "INDEX"),
        instrument("NIFTY-AUG-FUT", "FUTIDX", expiry=date(2026, 8, 27), lot_size=75),
        instrument("NIFTY-SEP-FUT", "FUTIDX", expiry=date(2026, 9, 24), lot_size=75),
    ]
    for expiry in (near, later):
        for strike in strikes:
            for option_type in ("CE", "PE"):
                instruments.append(
                    instrument(
                        f"NIFTY-{expiry}-{strike}-{option_type}",
                        "OPTIDX",
                        expiry=expiry,
                        strike=strike,
                        option_type=option_type,
                        lot_size=75,
                        tick_size=Decimal("0.05"),
                    )
                )

    universe = resolve_nifty_universe(instruments, Decimal("22182"), date(2026, 8, 10), window=2)

    assert universe.option_expiry == near
    assert universe.future.trading_symbol == "NIFTY-AUG-FUT"
    assert universe.atm_strike == Decimal("22150")
    assert {item.strike for item in universe.options} == set(strikes[1:6])
    assert {item.lot_size for item in universe.options} == {75}


def observation(identifier: str, second: int, price: str) -> MarketObservation:
    event_at = datetime(2026, 8, 10, 4, 0, second, tzinfo=UTC)
    return MarketObservation(
        observation_id=identifier,
        instrument="NIFTY",
        price=Decimal(price),
        event_at=event_at,
        received_at=event_at + timedelta(seconds=2),
    )


def test_candles_use_event_time_and_do_not_rewrite_late_intervals() -> None:
    builder = CandleBuilder()
    local_event = MarketObservation(
        observation_id="local",
        instrument="NIFTY-FUT",
        price=Decimal("100"),
        event_at=datetime(2026, 8, 10, 9, 30, 5, tzinfo=ZoneInfo("Asia/Kolkata")),
        received_at=datetime(2026, 8, 10, 9, 30, 6, tzinfo=ZoneInfo("Asia/Kolkata")),
    )
    assert local_event.event_at == datetime(2026, 8, 10, 4, 0, 5, tzinfo=UTC)
    assert builder.add(observation("close", 50, "101")) == "ACCEPTED"
    assert builder.add(observation("open", 5, "100")) == "ACCEPTED"
    assert builder.add(observation("high", 30, "103")) == "ACCEPTED"
    assert builder.add(observation("high", 30, "103")) == "DUPLICATE"

    candle = builder.finalize_before(datetime(2026, 8, 10, 4, 1, tzinfo=UTC))[0]
    assert (candle.open, candle.high, candle.low, candle.close) == (
        Decimal("100"),
        Decimal("103"),
        Decimal("100"),
        Decimal("101"),
    )
    assert candle.observation_ids == ("open", "high", "close")
    assert builder.add(observation("late", 40, "99")) == "LATE"
    assert builder.late_observations[0].observation_id == "late"


def test_five_minute_candle_requires_complete_finalized_provenance() -> None:
    start = datetime(2026, 8, 10, 4, 0, tzinfo=UTC)
    minutes = tuple(
        Candle(
            instrument="NIFTY",
            interval="1m",
            bucket_start=start + timedelta(minutes=offset),
            open=Decimal(100 + offset),
            high=Decimal(102 + offset),
            low=Decimal(99 + offset),
            close=Decimal(101 + offset),
            observation_ids=(f"event-{offset}",),
        )
        for offset in range(5)
    )

    aggregate = aggregate_five_minute(minutes)[0]
    assert (aggregate.open, aggregate.high, aggregate.low, aggregate.close) == (
        Decimal(100),
        Decimal(106),
        Decimal(99),
        Decimal(105),
    )
    assert aggregate.observation_ids == tuple(f"event-{offset}" for offset in range(5))
    assert aggregate_five_minute(minutes[:-1]) == ()
