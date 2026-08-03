"""Thread-isolated Groww feed consumption and normalized read-only persistence."""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Protocol

from pydantic import JsonValue

from trading_system.domain.models import Instrument
from trading_system.market.candles import CandleObservation
from trading_system.persistence.redis_state import (
    HeartbeatState,
    QuoteState,
    SubscriptionState,
)
from trading_system.services.readiness import ReadinessCoordinator


_RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_MAX_JITTER_FRACTION = 0.2
_SOURCE = "groww_live"
_FEED_SERVICE = "groww-feed"
_SUBSCRIPTION_KEY = "market-feed"


class GrowwFeed(Protocol):
    """Minimal Groww feed surface; no SDK or execution methods escape it."""

    def subscribe_index_value(
        self,
        instruments: list[dict[str, str]],
        on_data_received: Callable[[], None],
    ) -> object: ...

    def subscribe_ltp(
        self,
        instruments: list[dict[str, str]],
        on_data_received: Callable[[], None],
    ) -> object: ...

    def unsubscribe_index_value(self, instruments: list[dict[str, str]]) -> object: ...

    def unsubscribe_ltp(self, instruments: list[dict[str, str]]) -> object: ...

    def get_index_value(self) -> dict[str, Any]: ...

    def get_ltp(self) -> dict[str, Any]: ...

    def consume(self) -> None: ...


class FeedRepositoryPort(Protocol):
    async def save_observation(
        self,
        *,
        instrument_key: str,
        source: str,
        idempotency_key: str,
        observed_at: datetime,
        raw_payload: dict[str, Any],
        is_late: bool = False,
    ) -> bool: ...

    async def save_health_transition(
        self,
        *,
        service: str,
        state: str,
        source: str,
        idempotency_key: str,
        observed_at: datetime,
        raw_payload: dict[str, Any],
    ) -> bool: ...


class FeedRedisPort(Protocol):
    async def set_quote(self, instrument_key: str, value: QuoteState) -> None: ...

    async def set_heartbeat(self, service: str, value: HeartbeatState) -> None: ...

    async def set_subscription_state(
        self, subscription_key: str, value: SubscriptionState
    ) -> None: ...


class CandleBuilderPort(Protocol):
    def accept(self, observation: CandleObservation) -> object: ...


class ReadinessPort(Protocol):
    async def dependencies_ready(self) -> None: ...

    async def reconciled(self) -> None: ...

    async def first_live_update(self) -> None: ...

    async def dependency_blocked(self, dependency: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _FeedMessage:
    response: dict[str, Any]
    instruments: tuple[Instrument, ...]
    price_field: str
    generation: int


class GrowwFeedWorker:
    """Subscribe, normalize, persist, and reconnect without blocking an API loop."""

    def __init__(
        self,
        *,
        feed_factory: Callable[[], GrowwFeed],
        index: Instrument,
        derivatives: tuple[Instrument, ...],
        repository: FeedRepositoryPort,
        redis_state: FeedRedisPort,
        candle_builder: CandleBuilderPort,
        readiness: ReadinessPort | ReadinessCoordinator,
        sleeper: Callable[[float], Awaitable[None] | None] = asyncio.sleep,
        stale_sleeper: Callable[[float], Awaitable[None] | None] = asyncio.sleep,
        jitter: Callable[[float], float] = lambda delay: random.uniform(
            -delay * _MAX_JITTER_FRACTION, delay * _MAX_JITTER_FRACTION
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_after_seconds: int,
        reconcile: Callable[[], Awaitable[object]] | None = None,
        queue_capacity: int = 1024,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        self._feed_factory = feed_factory
        self._index = index
        self._derivatives = derivatives
        self._repository = repository
        self._redis_state = redis_state
        self._candle_builder = candle_builder
        self._readiness = readiness
        self._sleeper = sleeper
        self._stale_sleeper = stale_sleeper
        self._jitter = jitter
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds
        self._reconcile = reconcile
        self._active_feed: GrowwFeed | None = None
        self._index_subscribed = False
        self._derivatives_subscribed = False
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._last_valid_update_at: datetime | None = None
        self._subscription_established_at: datetime | None = None
        self._stale = False
        self._needs_reconciliation = False
        self._generation = 0
        self._active_generation: int | None = None
        self._baseline_generation: int | None = None
        self._generation_failed = False
        self._queue: asyncio.Queue[_FeedMessage] = asyncio.Queue(maxsize=queue_capacity)
        self._transition_lock = asyncio.Lock()
        self._latest_event_at: dict[str, datetime] = {}
        self._reconnect_attempt = 0

    async def run(self) -> None:
        """Run blocking feed consumption in a worker thread with reconnects."""
        if self._stopped:
            return
        monitor = asyncio.create_task(self._monitor_staleness())
        consumer = asyncio.create_task(self._consume_messages())
        try:
            loop = asyncio.get_running_loop()
            while not self._stopped:
                try:
                    if not await self._reconcile_if_required():
                        return
                    if self._stop_event.is_set():
                        return
                    feed = self._feed_factory()
                    if self._stop_event.is_set():
                        return
                    self._active_feed = feed
                    self._generation += 1
                    self._generation_failed = False
                    if not await self._subscribe(feed, loop):
                        return
                    if self._stop_event.is_set():
                        return
                    await asyncio.to_thread(feed.consume)
                    await asyncio.sleep(0)
                    await self._queue.join()
                    if self._stopped:
                        return
                    await self._mark_blocked()
                except Exception:
                    await self._mark_blocked()
                finally:
                    self._unsubscribe_active()
                if self._stopped:
                    return
                delay = _RECONNECT_DELAYS[
                    min(self._reconnect_attempt, len(_RECONNECT_DELAYS) - 1)
                ]
                self._reconnect_attempt += 1
                await self._sleep_with_jitter(delay)
        finally:
            monitor.cancel()
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
            with suppress(asyncio.CancelledError):
                await consumer

    async def stop(self) -> None:
        """Idempotently remove subscriptions and publish inactive worker intent."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        self._unsubscribe_active()
        await self._redis_state.set_subscription_state(
            _SUBSCRIPTION_KEY,
            SubscriptionState(
                subscription_key=_SUBSCRIPTION_KEY,
                observed_at=self._aware_now(),
                payload={
                    "state": "inactive",
                    "instrument_tokens": self._instrument_tokens(),
                },
            ),
        )

    async def mark_stale_if_silent(self) -> bool:
        """Persist one stale transition after valid callback silence exceeds the limit."""
        async with self._transition_lock:
            generation = self._active_generation
            baseline = self._last_valid_update_at or self._subscription_established_at
            if generation is None or self._baseline_generation != generation or baseline is None or self._stale:
                return False
            now = self._aware_now()
            silence_seconds = int((now - baseline).total_seconds())
            if silence_seconds <= self._stale_after_seconds:
                return False
        await self._redis_state.set_heartbeat(
            _FEED_SERVICE,
            HeartbeatState(
                service=_FEED_SERVICE,
                observed_at=now,
                payload={"state": "stale", "silence_seconds": silence_seconds},
            ),
        )
        await self._repository.save_health_transition(
            service=_FEED_SERVICE,
            state="stale",
            source="worker",
            idempotency_key=f"health:{_FEED_SERVICE}:stale:{int(now.timestamp() * 1000)}",
            observed_at=now,
            raw_payload={"silence_seconds": silence_seconds},
        )
        async with self._transition_lock:
            if self._active_generation != generation or self._baseline_generation != generation:
                return False
            self._needs_reconciliation = True
            self._generation_failed = True
            self._stale = True
            self._active_generation = None
        await self._readiness.dependency_blocked(_FEED_SERVICE)
        return True

    async def _subscribe(
        self, feed: GrowwFeed, loop: asyncio.AbstractEventLoop
    ) -> bool:
        index_wire = [self._wire_instrument(self._index, segment="CASH")]
        derivative_wire = [
            self._wire_instrument(instrument, segment="FNO")
            for instrument in self._derivatives
        ]
        feed.subscribe_index_value(
            index_wire,
            lambda: self._schedule_response(loop, feed.get_index_value, (self._index,), "value"),
        )
        self._index_subscribed = True
        if self._stop_event.is_set():
            self._unsubscribe_active()
            return False
        if derivative_wire:
            feed.subscribe_ltp(
                derivative_wire,
                lambda: self._schedule_response(
                    loop, feed.get_ltp, self._derivatives, "ltp"
                ),
            )
            self._derivatives_subscribed = True
            if self._stop_event.is_set():
                self._unsubscribe_active()
                return False
        await self._redis_state.set_subscription_state(
            _SUBSCRIPTION_KEY,
            SubscriptionState(
                subscription_key=_SUBSCRIPTION_KEY,
                observed_at=self._aware_now(),
                payload={
                    "state": "active",
                    "instrument_tokens": self._instrument_tokens(),
                },
            ),
        )
        if self._stop_event.is_set():
            self._unsubscribe_active()
            return False
        async with self._transition_lock:
            self._active_generation = self._generation
            self._baseline_generation = self._generation
            self._last_valid_update_at = None
            self._subscription_established_at = self._aware_now()
            self._stale = False
        return True

    def _schedule_response(
        self,
        loop: asyncio.AbstractEventLoop,
        getter: Callable[[], dict[str, Any]],
        instruments: tuple[Instrument, ...],
        price_field: str,
    ) -> None:
        try:
            response = getter()
        except Exception:
            loop.call_soon_threadsafe(self._signal_callback_failure)
            return
        message = _FeedMessage(response, instruments, price_field, self._generation)
        loop.call_soon_threadsafe(
            self._enqueue_message,
            message,
        )

    def _enqueue_message(self, message: _FeedMessage) -> None:
        if self._queue.full():
            self._signal_callback_failure()
            return
        self._queue.put_nowait(message)

    def _signal_callback_failure(self) -> None:
        if self._generation_failed:
            return
        self._generation_failed = True
        asyncio.create_task(self._mark_blocked())

    async def _consume_messages(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                if message.generation == self._generation and not self._generation_failed:
                    await self._handle_response(
                        message.response, message.instruments, message.price_field, message.generation
                    )
            except Exception:
                await self._mark_blocked()
            finally:
                self._queue.task_done()

    async def _handle_response(
        self,
        response: dict[str, Any],
        instruments: tuple[Instrument, ...],
        price_field: str,
        generation: int,
    ) -> None:
        async with self._transition_lock:
            if self._active_generation != generation:
                return
            await self._handle_response_serialized(response, instruments, price_field)

    async def _handle_response_serialized(
        self,
        response: dict[str, Any],
        instruments: tuple[Instrument, ...],
        price_field: str,
    ) -> None:
        for instrument in instruments:
            point = self._point_for(response, instrument)
            if point is None:
                continue
            normalized = self._normalize_point(instrument, point, price_field)
            if normalized is None:
                continue
            observed_at, price = normalized
            latest = self._latest_event_at.get(instrument.instrument_token)
            is_late = latest is not None and observed_at < latest
            raw_payload = {
                "raw": point,
                "normalized": {
                    "instrument_token": instrument.instrument_token,
                    "last_price": str(price),
                    "observed_at": observed_at.isoformat(),
                    "source": _SOURCE,
                },
            }
            inserted = await self._repository.save_observation(
                instrument_key=instrument.instrument_token,
                source=_SOURCE,
                idempotency_key=self._observation_key(
                    instrument.instrument_token, observed_at, price
                ),
                observed_at=observed_at,
                raw_payload=raw_payload,
                is_late=is_late,
            )
            if is_late or not inserted:
                continue
            self._latest_event_at[instrument.instrument_token] = observed_at
            await self._redis_state.set_quote(
                instrument.instrument_token,
                QuoteState(
                    instrument_key=instrument.instrument_token,
                    observed_at=observed_at,
                    payload={"last_price": str(price), "source": _SOURCE},
                ),
            )
            arrival = self._aware_now()
            self._last_valid_update_at = arrival
            self._stale = False
            await self._redis_state.set_heartbeat(
                _FEED_SERVICE,
                HeartbeatState(
                    service=_FEED_SERVICE,
                    observed_at=arrival,
                    payload={
                        "state": "live",
                        "last_instrument": instrument.instrument_token,
                    },
                ),
            )
            try:
                self._candle_builder.accept(
                    CandleObservation(
                        instrument_token=instrument.instrument_token,
                        observed_at=observed_at,
                        price=price,
                    )
                )
            except ValueError:
                # The observation remains durable; candle rules reject closed sessions.
                pass
            self._reconnect_attempt = 0
            await self._readiness.first_live_update()

    async def _mark_blocked(self) -> None:
        async with self._transition_lock:
            now = self._aware_now()
            self._needs_reconciliation = True
            self._generation_failed = True
            await self._redis_state.set_heartbeat(
                _FEED_SERVICE,
                HeartbeatState(
                    service=_FEED_SERVICE,
                    observed_at=now,
                    payload={"state": "blocked"},
                ),
            )
            await self._readiness.dependency_blocked(_FEED_SERVICE)

    def _unsubscribe_active(self) -> None:
        self._active_generation = None
        self._baseline_generation = None
        self._subscription_established_at = None
        self._last_valid_update_at = None
        if self._active_feed is None:
            return
        if self._index_subscribed:
            try:
                self._active_feed.unsubscribe_index_value(
                    [self._wire_instrument(self._index, segment="CASH")]
                )
            except Exception:
                pass
            self._index_subscribed = False
        derivative_wire = [
            self._wire_instrument(instrument, segment="FNO")
            for instrument in self._derivatives
        ]
        if derivative_wire and self._derivatives_subscribed:
            try:
                self._active_feed.unsubscribe_ltp(derivative_wire)
            except Exception:
                pass
            self._derivatives_subscribed = False
        self._active_feed = None

    async def _reconcile_if_required(self) -> bool:
        if not self._needs_reconciliation:
            return not self._stop_event.is_set()
        if self._reconcile is None:
            raise RuntimeError("reconciliation is required before feed recovery")
        await self._reconcile()
        if self._stop_event.is_set():
            return False
        await self._readiness.dependencies_ready()
        if self._stop_event.is_set():
            return False
        await self._readiness.reconciled()
        if self._stop_event.is_set():
            return False
        self._needs_reconciliation = False
        return True

    async def _sleep_with_jitter(self, delay: float) -> None:
        jitter = self._jitter(delay)
        bounded = min(
            max(jitter, -delay * _MAX_JITTER_FRACTION),
            delay * _MAX_JITTER_FRACTION,
        )
        result = self._sleeper(delay + bounded)
        if inspect.isawaitable(result):
            await result
        else:
            await asyncio.sleep(0)

    async def _monitor_staleness(self) -> None:
        interval = max(1.0, self._stale_after_seconds / 2)
        while not self._stopped:
            result = self._stale_sleeper(interval)
            if inspect.isawaitable(result):
                await result
            await self.mark_stale_if_silent()

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _instrument_tokens(self) -> list[JsonValue]:
        tokens: list[JsonValue] = [self._index.instrument_token]
        tokens.extend(instrument.instrument_token for instrument in self._derivatives)
        return tokens

    @staticmethod
    def _wire_instrument(instrument: Instrument, *, segment: str) -> dict[str, str]:
        return {
            "exchange": instrument.exchange,
            "segment": segment,
            "exchange_token": instrument.instrument_token,
        }

    @staticmethod
    def _point_for(
        response: dict[str, Any], instrument: Instrument
    ) -> dict[str, Any] | None:
        segment = "CASH" if instrument.instrument_type.strip().upper() == "INDEX" else "FNO"
        exchange_data = response.get(instrument.exchange)
        if not isinstance(exchange_data, dict):
            return None
        segment_data = exchange_data.get(segment)
        if not isinstance(segment_data, dict):
            return None
        point = segment_data.get(instrument.instrument_token)
        return point if isinstance(point, dict) else None

    @staticmethod
    def _normalize_point(
        instrument: Instrument, point: dict[str, Any], price_field: str
    ) -> tuple[datetime, Decimal] | None:
        del instrument
        timestamp = point.get("tsInMillis")
        raw_price = point.get(price_field)
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            return None
        try:
            price = Decimal(str(raw_price))
            observed_at = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
        except (InvalidOperation, TypeError, ValueError, OSError, OverflowError):
            return None
        if not price.is_finite() or price < 0:
            return None
        return observed_at, price

    @staticmethod
    def _observation_key(token: str, observed_at: datetime, price: Decimal) -> str:
        del price
        material = f"{token}\0{observed_at.isoformat()}"
        return f"feed:{sha256(material.encode()).hexdigest()}"


__all__ = ["GrowwFeed", "GrowwFeedWorker"]
