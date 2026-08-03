from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_system.market.candles import CandleBuilder, CandleObservation, aggregate_five_minute
from trading_system.market.session import NseSessionCalendar


def observation(at: str, price: str, volume: int | None = None) -> CandleObservation:
    return CandleObservation(
        instrument_token="NIFTY50",
        observed_at=datetime.fromisoformat(at),
        price=Decimal(price),
        volume=volume,
    )


@pytest.fixture
def builder() -> CandleBuilder:
    return CandleBuilder(NseSessionCalendar())


def test_late_observation_is_audited_without_rewriting_final_candle(builder):
    builder.accept(observation("2026-08-03T03:45:05+00:00", "100"))
    finalized = builder.finalize_through(datetime.fromisoformat("2026-08-03T03:46:00+00:00"))
    late = builder.accept(observation("2026-08-03T03:45:30+00:00", "120"))

    assert late.late is True
    assert late.late_observation is not None
    assert finalized[0].high == Decimal("100")


def test_minute_intervals_are_half_open_and_open_comes_from_event_time(builder):
    builder.accept(observation("2026-08-03T03:45:50+00:00", "102", 7))
    builder.accept(observation("2026-08-03T03:45:05+00:00", "100", 3))
    builder.accept(observation("2026-08-03T03:46:00+00:00", "103", 2))
    finalized = builder.finalize_through(datetime(2026, 8, 3, 3, 47, tzinfo=UTC))

    assert [(c.opened_at, c.open, c.close, c.volume) for c in finalized] == [
        (datetime(2026, 8, 3, 3, 45, tzinfo=UTC), Decimal("100"), Decimal("102"), 10),
        (datetime(2026, 8, 3, 3, 46, tzinfo=UTC), Decimal("103"), Decimal("103"), 2),
    ]


def test_identical_event_timestamps_use_acceptance_order_for_open_and_close(builder):
    builder.accept(observation("2026-08-03T03:45:05+00:00", "102", 1))
    builder.accept(observation("2026-08-03T03:45:05+00:00", "100", 1))

    candle = builder.finalize_through(datetime(2026, 8, 3, 3, 46, tzinfo=UTC))[0]

    assert (candle.open, candle.close, candle.high, candle.low) == (
        Decimal("102"), Decimal("100"), Decimal("102"), Decimal("100")
    )


def test_unavailable_volume_is_preserved_as_unavailable(builder):
    builder.accept(observation("2026-08-03T03:45:05+00:00", "100"))

    candle = builder.finalize_through(datetime(2026, 8, 3, 3, 46, tzinfo=UTC))[0]

    assert candle.volume is None


def test_five_minute_aggregate_needs_exactly_five_finalized_one_minute_candles(builder):
    for minute in range(5):
        builder.accept(observation(f"2026-08-03T03:{45 + minute:02}:05+00:00", str(100 + minute), 1))
    candles = builder.finalize_through(datetime(2026, 8, 3, 3, 50, tzinfo=UTC))

    aggregate = aggregate_five_minute(candles)

    assert (aggregate.open, aggregate.high, aggregate.low, aggregate.close, aggregate.volume) == (
        Decimal("100"), Decimal("104"), Decimal("100"), Decimal("104"), 5
    )


def test_five_minute_aggregate_rejects_incomplete_or_cross_session_candles(builder):
    builder.accept(observation("2026-08-03T03:45:05+00:00", "100", 1))
    candle = builder.finalize_through(datetime(2026, 8, 3, 3, 46, tzinfo=UTC))[0]

    with pytest.raises(ValueError, match="exactly five"):
        aggregate_five_minute((candle,))


def test_five_minute_aggregate_never_crosses_exchange_sessions(builder):
    for minute in range(5):
        builder.accept(observation(f"2026-08-03T03:{45 + minute:02}:05+00:00", "100", 1))
    candles = list(builder.finalize_through(datetime(2026, 8, 3, 3, 50, tzinfo=UTC)))
    candles[-1] = candles[-1].model_copy(update={"session_date": datetime(2026, 8, 4).date()})

    with pytest.raises(ValueError, match="one exchange session"):
        aggregate_five_minute(candles)
