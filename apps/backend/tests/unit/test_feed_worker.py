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
from trading_system.worker.feed import FeedSubscriptionSet, GrowwFeedWorker


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
    def __init__(self, *, observation_error: Exception | None = None) -> None:
        self.observations: dict[str, dict[str, Any]] = {}
        self.health: dict[str, dict[str, Any]] = {}
        self.observation_error = observation_error

    async def save_observation(self, **values: Any) -> bool:
        if self.observation_error is not None:
            raise self.observation_error
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


class DelayedFirstRepository(MemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.first = True

    async def save_observation(self, **values: Any) -> bool:
        if self.first:
            self.first = False
            self.started.set()
            await self.release.wait()
        return await super().save_observation(**values)


class PausedHealthRepository(MemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.health_started = asyncio.Event()
        self.health_release = asyncio.Event()

    async def save_health_transition(self, **values: Any) -> bool:
        self.health_started.set()
        await self.health_release.wait()
        return await super().save_health_transition(**values)

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


class SequenceIndexFeed(FakeFeed):
    def __init__(self, payloads: list[dict[str, Any]], *, release: Event | None = None) -> None:
        super().__init__(index_callback_count=len(payloads))
        self.payloads = payloads
        self.release = release

    def get_index_value(self) -> dict[str, Any]:
        return self.payloads.pop(0)

    def consume(self) -> None:
        super().consume()
        if self.release is not None and not self.release.wait(timeout=2):
            raise TimeoutError("test feed was not released")


class PartialSubscriptionFeed(FakeFeed):
    def subscribe_ltp(
        self, instruments: list[dict[str, str]], on_data_received: Callable[[], None]
    ) -> object:
        raise RuntimeError("derivative subscription failed")


class ControlledFeed(FakeFeed):
    def __init__(self, release: Event) -> None:
        super().__init__(index_payload=index_payload())
        self.release = release
        self.consume_calls = 0

    def consume(self) -> None:
        self.consume_calls += 1
        if not self.release.wait(timeout=2):
            raise TimeoutError("controlled feed was not released")

    def emit(self) -> None:
        assert self.index_callback is not None
        self.index_callback()


class PausedActiveRedis(MemoryRedisState):
    def __init__(self) -> None:
        super().__init__()
        self.active_started = asyncio.Event()
        self.active_release = asyncio.Event()

    async def set_subscription_state(
        self, subscription_key: str, value: SubscriptionState
    ) -> None:
        if value.payload.get("state") == "active":
            self.active_started.set()
            await self.active_release.wait()
        await super().set_subscription_state(subscription_key, value)


class PausedStaleRedis(MemoryRedisState):
    def __init__(self) -> None:
        super().__init__()
        self.stale_started = asyncio.Event()
        self.stale_release = asyncio.Event()

    async def set_heartbeat(self, service: str, value: HeartbeatState) -> None:
        if service == "groww-feed" and value.payload.get("state") == "stale":
            self.stale_started.set()
            await self.stale_release.wait()
        await super().set_heartbeat(service, value)


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


async def yield_until(predicate: Callable[[], bool], *, timeout_seconds: float = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"condition was not reached within {timeout_seconds} seconds")


async def successful_reconciliation() -> object:
    return object()


async def run_until_then_stop(worker: GrowwFeedWorker, predicate: Callable[[], bool]) -> None:
    task = asyncio.create_task(worker.run())
    await yield_until(predicate)
    await worker.stop()
    await asyncio.wait_for(task, timeout=1)


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
        reconcile=successful_reconciliation,
    )

    await run_until_then_stop(worker, lambda: len(repository.observations) == 2)

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
async def test_nearest_future_callback_populates_the_explicit_api_lookup_key() -> None:
    feed = FakeFeed(index_payload={}, ltp_payload=ltp_payload())
    repository = MemoryRepository()
    redis_state = MemoryRedisState()
    readiness = ReadinessCoordinator(redis_state, clock=lambda: AT)
    await readiness.dependencies_ready()
    await readiness.reconciled()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(FUTURE,),
        repository=repository,
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=readiness,
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
        reconcile=successful_reconciliation,
        quote_key=lambda item: (
            "nifty-nearest-future"
            if item.instrument_token == FUTURE.instrument_token
            else item.instrument_token
        ),
    )

    await run_until_then_stop(
        worker, lambda: "nifty-nearest-future" in redis_state.quotes
    )

    future_state = redis_state.quotes["nifty-nearest-future"]
    assert future_state.instrument_key == FUTURE.instrument_token


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

    await run_until_then_stop(worker, lambda: len(repository.observations) == 1)

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
    release = Event()
    feed = BlockingFeed(release)
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
    task = asyncio.create_task(worker.run())
    await yield_until(lambda: readiness.is_ready)
    assert readiness.is_ready is True

    now[0] = AT + timedelta(seconds=11)
    changed = await worker.mark_stale_if_silent()
    await worker.stop()
    release.set()
    await asyncio.wait_for(task, timeout=1)

    assert changed is True
    assert readiness.is_ready is False
    assert redis_state.heartbeats["groww-feed"] == HeartbeatState(
        service="groww-feed",
        observed_at=AT + timedelta(seconds=11),
        payload={"state": "stale", "silence_seconds": 11},
    )
    assert [value["state"] for value in repository.health.values()] == ["live", "stale"]


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
    await worker.stop()
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
    worker: GrowwFeedWorker

    async def stop_after_third_disconnect(delay: float) -> None:
        sleeps.append(delay)
        if not feeds:
            await worker.stop()

    worker = GrowwFeedWorker(
        feed_factory=lambda: feeds.pop(0),
        index=INDEX,
        derivatives=(),
        repository=MemoryRepository(),
        redis_state=MemoryRedisState(),
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(MemoryRedisState(), clock=lambda: AT),
        sleeper=stop_after_third_disconnect,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
        reconcile=successful_reconciliation,
    )

    await worker.run()

    assert sleeps == [1, 2, 4]


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
    task = asyncio.create_task(worker.run())
    await yield_until(lambda: "market-feed" in redis_state.subscriptions)
    await worker.stop()
    await asyncio.wait_for(task, timeout=1)
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


@pytest.mark.asyncio
async def test_zero_tick_subscription_becomes_stale_from_established_baseline() -> None:
    now = [AT]
    release = Event()
    feed = ControlledFeed(release)
    repository = MemoryRepository()
    redis_state = MemoryRedisState()
    readiness = ReadinessCoordinator(redis_state, clock=lambda: now[0])
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
    task = asyncio.create_task(worker.run())
    await yield_until(lambda: "market-feed" in redis_state.subscriptions)
    now[0] = AT + timedelta(seconds=11)

    assert await worker.mark_stale_if_silent() is True
    await worker.stop()
    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert redis_state.heartbeats["groww-feed"].payload["state"] == "stale"


@pytest.mark.asyncio
async def test_equivalent_price_encodings_are_one_logical_observation() -> None:
    first = index_payload()
    first["NSE"]["CASH"]["NIFTY50"]["value"] = "25001.50"
    second = index_payload()
    second["NSE"]["CASH"]["NIFTY50"]["value"] = 25001.5
    repository = MemoryRepository()
    worker_feed = SequenceIndexFeed([first, second])
    worker = GrowwFeedWorker(
        feed_factory=lambda: worker_feed,
        index=INDEX,
        derivatives=(),
        repository=repository,
        redis_state=MemoryRedisState(),
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(MemoryRedisState(), clock=lambda: AT),
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    await run_until_then_stop(
        worker, lambda: not worker_feed.payloads and len(repository.observations) == 1
    )

    assert len(repository.observations) == 1


@pytest.mark.asyncio
async def test_unexpected_consume_eof_blocks_cleans_up_and_reconnects() -> None:
    feeds = [FakeFeed(index_payload={}), FakeFeed(index_payload={})]
    sleeps: list[float] = []
    worker: GrowwFeedWorker

    async def stop_after_first_retry(delay: float) -> None:
        sleeps.append(delay)
        await worker.stop()

    worker = GrowwFeedWorker(
        feed_factory=lambda: feeds.pop(0),
        index=INDEX,
        derivatives=(),
        repository=MemoryRepository(),
        redis_state=(redis_state := MemoryRedisState()),
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        sleeper=stop_after_first_retry,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    await worker.run()

    assert sleeps == [1]
    assert redis_state.heartbeats["groww-feed"].payload["state"] == "blocked"
    assert len(feeds) == 1


@pytest.mark.asyncio
async def test_partial_subscription_failure_cleans_index_before_retry() -> None:
    first = PartialSubscriptionFeed()
    worker: GrowwFeedWorker
    sleeps: list[float] = []

    async def stop_retry(delay: float) -> None:
        sleeps.append(delay)
        await worker.stop()

    worker = GrowwFeedWorker(
        feed_factory=lambda: first,
        index=INDEX,
        derivatives=(FUTURE,),
        repository=MemoryRepository(),
        redis_state=MemoryRedisState(),
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(MemoryRedisState(), clock=lambda: AT),
        sleeper=stop_retry,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    await worker.run()

    assert sleeps == [1]
    assert len(first.index_unsubscriptions) == 1


@pytest.mark.asyncio
async def test_callback_persistence_error_blocks_before_consume_returns() -> None:
    release = Event()
    redis_state = MemoryRedisState()
    worker = GrowwFeedWorker(
        feed_factory=lambda: BlockingFeed(release),
        index=INDEX,
        derivatives=(),
        repository=MemoryRepository(observation_error=RuntimeError("database failed")),
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    task = asyncio.create_task(worker.run())
    await yield_until(
        lambda: redis_state.heartbeats.get("groww-feed") is not None
        and redis_state.heartbeats["groww-feed"].payload.get("state") == "blocked"
    )
    await worker.stop()
    release.set()
    await asyncio.wait_for(task, timeout=1)

    assert redis_state.heartbeats["groww-feed"].payload["state"] == "blocked"


@pytest.mark.asyncio
async def test_disconnect_reconciles_before_reconnect_tick_restores_shared_ready() -> None:
    release = Event()
    feeds = [
        FakeFeed(consume_error=RuntimeError("feed disconnected")),
        BlockingFeed(release),
    ]
    redis_state = MemoryRedisState()
    readiness = ReadinessCoordinator(redis_state, clock=lambda: AT)
    await readiness.dependencies_ready()
    await readiness.reconciled()
    await readiness.first_live_update()
    reconciliations = 0

    async def reconcile() -> object:
        nonlocal reconciliations
        reconciliations += 1
        return object()

    worker = GrowwFeedWorker(
        feed_factory=lambda: feeds.pop(0),
        index=INDEX,
        derivatives=(),
        repository=MemoryRepository(),
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=readiness,
        reconcile=reconcile,
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    task = asyncio.create_task(worker.run())
    await yield_until(lambda: reconciliations == 1 and readiness.is_ready)
    shared = await redis_state.get_heartbeat("readiness")
    await worker.stop()
    release.set()
    await asyncio.wait_for(task, timeout=1)

    assert shared is not None
    assert shared.payload["ready"] is True
    assert reconciliations == 1


@pytest.mark.asyncio
async def test_reconnect_replaces_expired_future_subscription_and_lookup_mapping() -> None:
    rolled_future = instrument(
        "NIFTY-SEP-FUT", "FUTIDX", expiry=date(2026, 9, 24)
    )
    release = Event()
    first_feed = FakeFeed(consume_error=RuntimeError("feed disconnected"))
    second_feed = ControlledFeed(release)
    feeds = [first_feed, second_feed]
    redis_state = MemoryRedisState()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feeds.pop(0),
        index=INDEX,
        derivatives=(FUTURE,),
        repository=MemoryRepository(),
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        reconcile=lambda: asyncio.sleep(
            0,
            result=FeedSubscriptionSet(
                index=INDEX,
                derivatives=(rolled_future,),
                quote_key=lambda item: (
                    "nifty-nearest-future"
                    if item.instrument_token == rolled_future.instrument_token
                    else item.instrument_token
                ),
            ),
        ),
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    task = asyncio.create_task(worker.run())
    await yield_until(lambda: bool(second_feed.ltp_subscriptions))
    await worker.stop()
    release.set()
    await asyncio.wait_for(task, timeout=1)

    assert first_feed.ltp_subscriptions[0][0]["exchange_token"] == "NIFTY-FUT"
    assert second_feed.ltp_subscriptions[0] == [
        {"exchange": "NSE", "segment": "FNO", "exchange_token": "NIFTY-SEP-FUT"}
    ]
    assert worker._quote_key(rolled_future) == "nifty-nearest-future"

@pytest.mark.asyncio
async def test_failed_reconciliation_never_restores_or_resubscribes() -> None:
    feed_calls = 0
    attempts = 0
    worker: GrowwFeedWorker
    redis_state = MemoryRedisState()
    readiness = ReadinessCoordinator(redis_state, clock=lambda: AT)
    await readiness.dependencies_ready()
    await readiness.reconciled()
    await readiness.first_live_update()

    def feed_factory() -> FakeFeed:
        nonlocal feed_calls
        feed_calls += 1
        return FakeFeed(consume_error=RuntimeError("feed disconnected"))

    async def reconcile() -> object:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("account remains unavailable")

    async def stop_after_failure(_: float) -> None:
        if attempts:
            await worker.stop()

    worker = GrowwFeedWorker(
        feed_factory=feed_factory,
        index=INDEX,
        derivatives=(),
        repository=MemoryRepository(),
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=readiness,
        reconcile=reconcile,
        sleeper=stop_after_failure,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    await worker.run()

    assert readiness.is_ready is False
    assert attempts == 1
    assert feed_calls == 1


@pytest.mark.asyncio
async def test_serial_consumer_completes_high_callback_count_without_losing_events() -> None:
    payloads: list[dict[str, Any]] = []
    for sequence in range(200):
        payload = index_payload()
        payload["NSE"]["CASH"]["NIFTY50"] = {
            "tsInMillis": 1785748500000 + sequence,
            "value": 25000 + sequence,
        }
        payloads.append(payload)
    feed = SequenceIndexFeed(payloads)
    repository = MemoryRepository()
    redis_state = MemoryRedisState()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(),
        repository=repository,
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
        queue_capacity=256,
    )

    await run_until_then_stop(worker, lambda: len(repository.observations) == 200)

    assert len(repository.observations) == 200
    assert redis_state.quotes["NIFTY50"].observed_at == datetime(
        2026, 8, 3, 9, 15, 0, 199000, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_older_event_cannot_overwrite_newer_quote_after_slow_persistence() -> None:
    newer = index_payload()
    newer["NSE"]["CASH"]["NIFTY50"] = {
        "tsInMillis": 1785748501000,
        "value": 25002,
    }
    older = index_payload()
    older["NSE"]["CASH"]["NIFTY50"] = {
        "tsInMillis": 1785748500000,
        "value": 25001,
    }
    consume_release = Event()
    feed = SequenceIndexFeed([newer, older], release=consume_release)
    repository = DelayedFirstRepository()
    redis_state = MemoryRedisState()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(),
        repository=repository,
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    task = asyncio.create_task(worker.run())
    await repository.started.wait()
    repository.release.set()
    await yield_until(lambda: len(repository.observations) == 2)
    await worker.stop()
    consume_release.set()
    await asyncio.wait_for(task, timeout=1)

    assert redis_state.quotes["NIFTY50"] == QuoteState(
        instrument_key="NIFTY50",
        observed_at=datetime(2026, 8, 3, 9, 15, 1, tzinfo=UTC),
        payload={"last_price": "25002", "source": "groww_live"},
    )


@pytest.mark.asyncio
async def test_stop_during_paused_reconciliation_never_resubscribes() -> None:
    reconcile_started = asyncio.Event()
    reconcile_release = asyncio.Event()
    feed_calls = 0

    def factory() -> FakeFeed:
        nonlocal feed_calls
        feed_calls += 1
        return FakeFeed(consume_error=RuntimeError("disconnect"))

    async def reconcile() -> object:
        reconcile_started.set()
        await reconcile_release.wait()
        return object()

    worker = GrowwFeedWorker(
        feed_factory=factory,
        index=INDEX,
        derivatives=(),
        repository=MemoryRepository(),
        redis_state=(redis_state := MemoryRedisState()),
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        reconcile=reconcile,
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    task = asyncio.create_task(worker.run())
    await reconcile_started.wait()
    await worker.stop()
    reconcile_release.set()
    await asyncio.wait_for(task, timeout=1)

    assert feed_calls == 1


@pytest.mark.asyncio
async def test_stop_during_active_state_persistence_cleans_without_consume() -> None:
    redis_state = PausedActiveRedis()
    feed = ControlledFeed(Event())
    worker = GrowwFeedWorker(
        feed_factory=lambda: feed,
        index=INDEX,
        derivatives=(),
        repository=MemoryRepository(),
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=ReadinessCoordinator(redis_state, clock=lambda: AT),
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        stale_after_seconds=10,
    )

    task = asyncio.create_task(worker.run())
    await redis_state.active_started.wait()
    await worker.stop()
    redis_state.active_release.set()
    await asyncio.wait_for(task, timeout=1)

    assert feed.consume_calls == 0
    assert len(feed.index_unsubscriptions) == 1
    assert redis_state.subscriptions["market-feed"].payload["state"] == "inactive"


@pytest.mark.asyncio
async def test_old_stale_transition_cannot_block_new_generation_tick() -> None:
    now = [AT]
    old_release = Event()
    new_release = Event()
    old_feed = BlockingFeed(old_release)
    new_feed = ControlledFeed(new_release)
    new_feed.index_payload["NSE"]["CASH"]["NIFTY50"]["tsInMillis"] = 1785748501123
    feeds = [old_feed, new_feed]
    repository = MemoryRepository()
    redis_state = PausedStaleRedis()
    readiness = ReadinessCoordinator(redis_state, clock=lambda: now[0])
    await readiness.dependencies_ready()
    await readiness.reconciled()
    worker = GrowwFeedWorker(
        feed_factory=lambda: feeds.pop(0),
        index=INDEX,
        derivatives=(),
        repository=repository,
        redis_state=redis_state,
        candle_builder=RecordingCandleBuilder(),
        readiness=readiness,
        reconcile=successful_reconciliation,
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: now[0],
        stale_after_seconds=10,
    )

    task = asyncio.create_task(worker.run())
    await yield_until(lambda: readiness.is_ready)
    now[0] = AT + timedelta(seconds=11)
    stale_task = asyncio.create_task(worker.mark_stale_if_silent())
    await redis_state.stale_started.wait()
    old_release.set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert new_feed.index_callback is None
    redis_state.stale_release.set()
    await stale_task
    await yield_until(lambda: new_feed.index_callback is not None)
    new_feed.emit()
    await yield_until(
        lambda: readiness.is_ready
        and redis_state.quotes["NIFTY50"].observed_at.second == 1
    )
    await worker.stop()
    new_release.set()
    await asyncio.wait_for(task, timeout=1)

    assert readiness.is_ready is True
    assert redis_state.heartbeats["groww-feed"].payload["state"] == "live"
    assert list(repository.health.values())[-1]["state"] == "live"
