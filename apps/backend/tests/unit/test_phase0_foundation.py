from datetime import datetime, timedelta, timezone
from decimal import Decimal
import threading

import pytest

from trading_system.groww import GrowwReadClient
from trading_system.ingestion import IngestionItem, IngestionOverflow, IngestionQueue
from trading_system.market import CandleBuilder, Session, SessionState, aggregate_candles
from trading_system.models import (
    CandleCompleteness,
    DataSource,
    EventOutcome,
    Instrument,
    MarketObservation,
    PersistedEvent,
)
from trading_system.quality import assess_quality

UTC = timezone.utc


def observation(offset: int) -> MarketObservation:
    at = datetime(2026, 8, 10, 3, 45, tzinfo=UTC) + timedelta(minutes=offset, seconds=1)
    return MarketObservation(
        observation_id=f"event-{offset}",
        instrument="NIFTY",
        price=Decimal(100 + offset),
        event_at=at,
        received_at=at,
    )


def test_candle_reconstruction_and_15m_completeness_are_deterministic() -> None:
    def build(source: DataSource):
        builder = CandleBuilder(source)
        for offset in range(15):
            assert builder.add(observation(offset)) == "ACCEPTED"
        return builder.finalize_before(datetime(2026, 8, 10, 4, 1, tzinfo=UTC))

    live = build(DataSource.LIVE_ACCEPTED)
    rebuilt = build(DataSource.DURABLE_RECONSTRUCTED)
    assert [(c.open, c.high, c.low, c.close) for c in live] == [
        (c.open, c.high, c.low, c.close) for c in rebuilt
    ]
    complete = aggregate_candles(live, 15)[0]
    incomplete = aggregate_candles((*live[:7], *live[8:]), 15)[0]
    assert complete.completeness is CandleCompleteness.COMPLETE and complete.actual_components == 15
    assert incomplete.completeness is CandleCompleteness.INCOMPLETE
    assert not incomplete.finalized and incomplete.actual_components == 14


def test_shared_quality_blocks_future_and_marks_required_stale() -> None:
    now = datetime(2026, 8, 10, 4, tzinfo=UTC)
    future = assess_quality(
        session=Session(SessionState.OPEN),
        now=now,
        timestamps={"spot": now + timedelta(seconds=6)},
        thresholds={"spot": 15},
        required=("spot",),
        max_future_seconds=5,
    )
    stale = assess_quality(
        session=Session(SessionState.OPEN),
        now=now,
        timestamps={"spot": now - timedelta(seconds=16)},
        thresholds={"spot": 15},
        required=("spot",),
    )
    assert str(future.status) == "BLOCKED" and future.issues[0].code == "TIMESTAMP_FUTURE"
    assert str(stale.status) == "STALE" and stale.issues[0].age_ms == 16000


def test_bounded_ingestion_persists_before_publish_and_overflow_is_explicit() -> None:
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []

    class Store:
        def append_events(self, events):
            entered.set()
            release.wait(2)
            order.append("persist")
            return len(events)

    event = PersistedEvent(
        kind="market_observation",
        source="GROWW",
        idempotency_key="one",
        event_identity="one",
        event_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        processed_at=datetime.now(UTC),
        raw={},
        normalized={},
        outcome=EventOutcome.ACCEPTED,
    )
    ingestion = IngestionQueue(Store(), capacity=1, batch_size=1)
    ingestion.submit(IngestionItem(event, lambda: order.append("publish")))
    assert entered.wait(1)
    ingestion.submit(IngestionItem(event))
    with pytest.raises(IngestionOverflow):
        ingestion.submit(IngestionItem(event))
    release.set()
    ingestion.join()
    ingestion.close()
    assert order[:2] == ["persist", "publish"]
    assert ingestion.metrics.overflowed == 1


def test_ingestion_failure_releases_nonduplicate_claim_for_retry() -> None:
    released: list[str] = []

    class Store:
        def append_events(self, _events):
            raise OSError("database unavailable")

        def release_event_claim(self, identity):
            released.append(identity)

    now = datetime.now(UTC)
    event = PersistedEvent(
        kind="market_observation",
        source="GROWW",
        idempotency_key="retryable",
        event_identity="retryable",
        event_at=now,
        received_at=now,
        processed_at=now,
        raw={},
        normalized={},
        outcome=EventOutcome.ACCEPTED,
    )
    ingestion = IngestionQueue(Store(), capacity=1, batch_size=1)
    ingestion.submit(IngestionItem(event))
    with pytest.raises(RuntimeError, match="persistence worker failed"):
        ingestion.join()
    assert released == ["retryable"]


def test_historical_interface_preserves_broker_provenance(monkeypatch) -> None:
    monkeypatch.setattr("trading_system.groww.metadata.version", lambda _name: "1.5.0")

    class SDK:
        def get_historical_candles(self, **kwargs):
            assert kwargs["candle_interval"] == "15minute"
            return {"candles": [["2026-08-10T09:15:00", 100, 102, 99, 101, 50, None]]}

    instrument = Instrument(
        trading_symbol="NIFTY",
        groww_symbol="NSE-NIFTY",
        exchange_token="1",
        exchange="NSE",
        segment="CASH",
        instrument_type="IDX",
        underlying_symbol="NIFTY",
    )
    candles = GrowwReadClient(SDK()).historical_candles(
        instrument,
        datetime(2026, 8, 10, 3, 45, tzinfo=UTC),
        datetime(2026, 8, 10, 4, tzinfo=UTC),
        "15m",
    )
    assert len(candles) == 1
    assert candles[0].source is DataSource.GROWW_HISTORICAL
    assert candles[0].bucket_start == datetime(2026, 8, 10, 3, 45, tzinfo=UTC)
