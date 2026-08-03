from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event
from typing import Any, Callable

import pytest

from trading_system.domain.models import Instrument, MarketDataSource
from trading_system.market.candles import CandleObservation
from trading_system.persistence.redis_state import (
    HeartbeatState,
    QuoteState,
    SubscriptionState,
)
from trading_system.services.readiness import ReadinessCoordinator
from trading_system.worker.feed import GrowwFeedWorker


AT = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)


def instrument(token: str, instrument_type: str, *, expiry: date | None = None) -> Instrument:
    return Instrument(
        instrument_token=token,
        symbol="NIFTY",
        exchange="NSE",
        instrument_type=instrument_type,
        expiry=expiry,
        lot_size=1 if instrument_type == "INDEX" else 75,
        tick_size=Decimal("0.05"),
        source=MarketDataSource.GROWW_LIVE,
    )


INDEX = instrument("NIFTY50", "INDEX")
FUTURE = instrument("NIFTY-FUT", "FUTIDX", expiry=date(2026, 8, 27))


class MemoryRedisState:
    def __init__(self) -> None:
        self.quotes: dict[str, QuoteState] = {}
        self.heartbeats: dict[str, HeartbeatState] = {}
        self.subscriptions: dict[str, SubscriptionState] = {}

    async def set_quote(self, instrument_key: str, value: QuoteState) -> None:
        self.quotes[instrument_key] = value

    async def set_heartbeat(self, service: str, value: HeartbeatState) -> None:
        self.heartbeats[service] = value

    async def get_heartbeat(self, service: str) -> HeartbeatState | None:
        return self.heartbeats.get(service)

    async def set_subscription_state(
        self, subscription_key: str, value: SubscriptionState
    ) -> None:
        self.subscriptions[subscription_key] = value


class MemoryRepository:
    def __init__(self) -> None:
        self.observations: dict[str, dict[str, Any]] = {}
        self.health: dict[str, dict[str, Any]] = {}

    async def save_observation(self, **values: Any) -> bool:
        key = str(values["idempotency_key"])
        if key in self.observations:
            return False
        self.observations[key] = values
        return True

    async def save_health_transition(self, **values: Any) -> bool:
        key = str(values["idempotency_key"])
        if key in self.health:
            return False
        self.health[key] = values
        return True


class RecordingCandleBuilder:
    def __init__(self) -> None:
        self.observations: list[CandleObservation] = []

    def accept(self, observation: CandleObservation) -> None:
        self.observations.append(observation)


class FakeFeed:
    def __init__(
        self,
        *,
        index_payload: dict[str, Any] | None = None,
        ltp_payload: dict[str, Any] | None = None,
        index_callback_count: int = 1,
        consume_error: Exception | None = None,
    ) -> None:
        self.index_payload = index_payload or {}
        self.ltp_payload = ltp_payload or {}
        self.index_callback_count = index_callback_count
        self.consume_error = consume_error
        self.index_callback: Callable[[], None] | None = None
        self.ltp_callback: Callable[[], None] | None = None
        self.index_subscriptions: list[list[dict[str, str]]] = []
        self.ltp_subscriptions: list[list[dict[str, str]]] = []
        self.index_unsubscriptions: list[list[dict[str, str]]] = []
        self.ltp_unsubscriptions: list[list[dict[str, str]]] = []

    def subscribe_index_value(
        self, instruments: list[dict[str, str]], on_data_received: Callable[[], None]
    ) -> object:
        self.index_subscriptions.append(instruments)
        self.index_callback = on_data_received
        return {"subscribed": True}

    def subscribe_ltp(
        self, instruments: list[dict[str, str]], on_data_received: Callable[[], None]
    ) -> object:
        self.ltp_subscriptions.append(instruments)
        self.ltp_callback = on_data_received
        return {"subscribed": True}

    def unsubscribe_index_value(self, instruments: list[dict[str, str]]) -> object:
        self.index_unsubscriptions.append(instruments)
        return {"unsubscribed": True}

    def unsubscribe_ltp(self, instruments: list[dict[str, str]]) -> object:
        self.ltp_unsubscriptions.append(instruments)
        return {"unsubscribed": True}

    def get_index_value(self) -> dict[str, Any]:
        return self.index_payload

    def get_ltp(self) -> dict[str, Any]:
        return self.ltp_payload

    def consume(self) -> None:
        if self.consume_error is not None:
            raise self.consume_error
        if self.index_callback is not None:
            for _ in range(self.index_callback_count):
                self.index_callback()
        if self.ltp_callback is not None and self.ltp_payload:
            self.ltp_callback()


class BlockingFeed(FakeFeed):
    def __init__(self, release: Event) -> None:
        super().__init__(index_payload=index_payload())
        self.release = release

    def consume(self) -> None:
        if self.index_callback is not None:
            self.index_callback()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test feed was not released")


def index_payload() -> dict[str, Any]:
    return {
        "NSE": {
            "CASH": {
                "NIFTY50": {"tsInMillis": 1785748500123, "value": 25001.5}
            }
        }
    }


def ltp_payload() -> dict[str, Any]:
    return {
        "NSE": {
            "FNO": {
                "NIFTY-FUT": {"tsInMillis": 1785748500456, "ltp": 25020.25}
            }
        }
    }


@pytest.mark.asyncio
async def test_callbacks_normalize_index_value_and_derivative_ltp_to_utc() -> None:
    feed = FakeFeed(index_payload=index_payload(), ltp_payload=ltp_payload())
    repository = MemoryRepository()
    redis_state = MemoryRedisState()
    candles = RecordingCandleBuilder()
    readiness = ReadinessCoordinator(redis_state, clock=lambda: AT)
    await readiness.dependencies_ready()
    await readiness.reconciled()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(FUTURE,),
        repository=repository,
        redis_state=redis_state,
        candle_builder=candles,
        readiness=readiness,
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    await worker.run()

    assert feed.index_subscriptions == [
        [{"exchange": "NSE", "segment": "CASH", "exchange_token": "NIFTY50"}]
    ]
    assert feed.ltp_subscriptions == [
        [{"exchange": "NSE", "segment": "FNO", "exchange_token": "NIFTY-FUT"}]
    ]
    assert redis_state.quotes["NIFTY50"] == QuoteState(
        instrument_key="NIFTY50",
        observed_at=datetime(2026, 8, 3, 9, 15, 0, 123000, tzinfo=UTC),
        payload={"last_price": "25001.5", "source": "groww_live"},
    )
    assert redis_state.quotes["NIFTY-FUT"] == QuoteState(
        instrument_key="NIFTY-FUT",
        observed_at=datetime(2026, 8, 3, 9, 15, 0, 456000, tzinfo=UTC),
        payload={"last_price": "25020.25", "source": "groww_live"},
    )
    assert candles.observations == [
        CandleObservation(
            instrument_token="NIFTY50",
            observed_at=datetime(2026, 8, 3, 9, 15, 0, 123000, tzinfo=UTC),
            price=Decimal("25001.5"),
        ),
        CandleObservation(
            instrument_token="NIFTY-FUT",
            observed_at=datetime(2026, 8, 3, 9, 15, 0, 456000, tzinfo=UTC),
            price=Decimal("25020.25"),
        ),
    ]
    durable_payloads = [row["raw_payload"] for row in repository.observations.values()]
    assert durable_payloads == [
        {
            "raw": {"tsInMillis": 1785748500123, "value": 25001.5},
            "normalized": {
                "instrument_token": "NIFTY50",
                "last_price": "25001.5",
                "observed_at": "2026-08-03T09:15:00.123000+00:00",
                "source": "groww_live",
            },
        },
        {
            "raw": {"tsInMillis": 1785748500456, "ltp": 25020.25},
            "normalized": {
                "instrument_token": "NIFTY-FUT",
                "last_price": "25020.25",
                "observed_at": "2026-08-03T09:15:00.456000+00:00",
                "source": "groww_live",
            },
        },
    ]
    assert readiness.is_ready is True


@pytest.mark.asyncio
async def test_duplicate_callback_remains_one_logical_durable_and_candle_event() -> None:
    feed = FakeFeed(index_payload=index_payload(), index_callback_count=2)
    repository = MemoryRepository()
    redis_state = MemoryRedisState()
    candles = RecordingCandleBuilder()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(),
        repository=repository,
        redis_state=redis_state,
        candle_builder=candles,
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    await worker.run()

    assert len(repository.observations) == 1
    assert candles.observations == [
        CandleObservation(
            instrument_token="NIFTY50",
            observed_at=datetime(2026, 8, 3, 9, 15, 0, 123000, tzinfo=UTC),
            price=Decimal("25001.5"),
        )
    ]
    assert redis_state.heartbeats["groww-feed"].payload == {
        "state": "live",
        "last_instrument": "NIFTY50",
    }


@pytest.mark.asyncio
async def test_callback_silence_marks_shared_state_stale_and_resets_readiness() -> None:
    now = [AT]
    feed = FakeFeed(index_payload=index_payload())
    repository = MemoryRepository()
    redis_state = MemoryRedisState()
    readiness = ReadinessCoordinator(redis_state, clock=lambda: now[0])
    await readiness.dependencies_ready()
    await readiness.reconciled()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(),
        repository=repository,
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=readiness,
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: now[0],
        stale_after_seconds=10,
    )
    await worker.run()
    assert readiness.is_ready is True

    now[0] = AT + timedelta(seconds=11)
    changed = await worker.mark_stale_if_silent()

    assert changed is True
    assert readiness.is_ready is False
    assert redis_state.heartbeats["groww-feed"] == HeartbeatState(
        service="groww-feed",
        observed_at=AT + timedelta(seconds=11),
        payload={"state": "stale", "silence_seconds": 11},
    )
    assert len(repository.health) == 1


@pytest.mark.asyncio
async def test_run_monitors_callback_silence_while_blocking_consume_is_active() -> None:
    now = [AT]
    release = Event()
    feed = BlockingFeed(release)
    repository = MemoryRepository()
    redis_state = MemoryRedisState()

    async def advance_after_first_callback(_: float) -> None:
        while redis_state.heartbeats.get("groww-feed") is None:
            await asyncio.sleep(0)
        now[0] = AT + timedelta(seconds=11)
        await asyncio.sleep(0)

    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(),
        repository=repository,
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: now[0]),
        sleeper=lambda _: None,
        stale_sleeper=advance_after_first_callback,
        jitter=lambda _: 0,
        clock=lambda: now[0],
        stale_after_seconds=10,
    )

    run_task = asyncio.create_task(worker.run())
    for _ in range(1000):
        heartbeat = redis_state.heartbeats.get("groww-feed")
        if heartbeat is not None and heartbeat.payload.get("state") == "stale":
            break
        await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(run_task, timeout=1)

    assert redis_state.heartbeats["groww-feed"].payload == {
        "state": "stale",
        "silence_seconds": 11,
    }


@pytest.mark.asyncio
async def test_reconnect_uses_bounded_exponential_delays() -> None:
    feeds = [
        FakeFeed(consume_error=RuntimeError("secret upstream text one")),
        FakeFeed(consume_error=RuntimeError("secret upstream text two")),
        FakeFeed(index_payload={}),
    ]
    sleeps: list[float] = []
    worker = GrowwFeedWorker(
        feed_factory=lambda: feeds.pop(0),
        index=INDEX,
        derivatives=(),
        repository=MemoryRepository(),
        redis_state=MemoryRedisState(),
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(MemoryRedisState(), clock=lambda: AT),
        sleeper=lambda delay: sleeps.append(delay),
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    await worker.run()

    assert sleeps == [1, 2]


@pytest.mark.asyncio
async def test_stop_unsubscribes_once_and_persists_inactive_intent() -> None:
    feed = FakeFeed(index_payload={})
    redis_state = MemoryRedisState()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(FUTURE,),
        repository=MemoryRepository(),
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )
    await worker.run()

    await worker.stop()
    await worker.stop()

    assert feed.index_unsubscriptions == [
        [{"exchange": "NSE", "segment": "CASH", "exchange_token": "NIFTY50"}]
    ]
    assert feed.ltp_unsubscriptions == [
        [{"exchange": "NSE", "segment": "FNO", "exchange_token": "NIFTY-FUT"}]
    ]
    assert redis_state.subscriptions["market-feed"] == SubscriptionState(
        subscription_key="market-feed",
        observed_at=AT,
        payload={"state": "inactive", "instrument_tokens": ["NIFTY50", "NIFTY-FUT"]},
    )
