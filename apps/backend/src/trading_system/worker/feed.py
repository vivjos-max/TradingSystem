"""Thread-isolated Groww feed consumption and normalized read-only persistence."""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from threading import Lock
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
    async def first_live_update(self) -> None: ...

    async def dependency_blocked(self, dependency: str) -> None: ...


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
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
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
        self._active_feed: GrowwFeed | None = None
        self._subscribed = False
        self._stopped = False
        self._last_valid_update_at: datetime | None = None
        self._stale = False
        self._callback_futures: list[Future[None]] = []
        self._callback_lock = Lock()

    async def run(self) -> None:
        """Run blocking feed consumption in a worker thread with reconnects."""
        if self._stopped:
            return
        monitor = asyncio.create_task(self._monitor_staleness())
        try:
            loop = asyncio.get_running_loop()
            reconnect_attempt = 0
            while not self._stopped:
                feed = self._feed_factory()
                self._active_feed = feed
                await self._subscribe(feed, loop)
                try:
                    await asyncio.to_thread(feed.consume)
                    await self._drain_callbacks()
                except Exception:
                    await self._drain_callbacks()
                    await self._mark_blocked()
                    self._unsubscribe_active()
                    if self._stopped:
                        return
                    delay = _RECONNECT_DELAYS[
                        min(reconnect_attempt, len(_RECONNECT_DELAYS) - 1)
                    ]
                    reconnect_attempt += 1
                    await self._sleep_with_jitter(delay)
                    continue
                return
        finally:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor

    async def stop(self) -> None:
        """Idempotently remove subscriptions and publish inactive worker intent."""
        if self._stopped:
            return
        self._stopped = True
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
        if self._last_valid_update_at is None or self._stale:
            return False
        now = self._aware_now()
        silence_seconds = int((now - self._last_valid_update_at).total_seconds())
        if silence_seconds <= self._stale_after_seconds:
            return False
        self._stale = True
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
        await self._readiness.dependency_blocked(_FEED_SERVICE)
        return True

    async def _subscribe(
        self, feed: GrowwFeed, loop: asyncio.AbstractEventLoop
    ) -> None:
        index_wire = [self._wire_instrument(self._index, segment="CASH")]
        derivative_wire = [
            self._wire_instrument(instrument, segment="FNO")
            for instrument in self._derivatives
        ]
        feed.subscribe_index_value(
            index_wire,
            lambda: self._schedule_response(loop, feed.get_index_value, (self._index,), "value"),
        )
        if derivative_wire:
            feed.subscribe_ltp(
                derivative_wire,
                lambda: self._schedule_response(
                    loop, feed.get_ltp, self._derivatives, "ltp"
                ),
            )
        self._subscribed = True
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
            return
        future = asyncio.run_coroutine_threadsafe(
            self._handle_response(response, instruments, price_field), loop
        )
        with self._callback_lock:
            self._callback_futures.append(future)

    async def _drain_callbacks(self) -> None:
        while True:
            with self._callback_lock:
                pending = self._callback_futures
                self._callback_futures = []
            if not pending:
                return
            await asyncio.gather(*(asyncio.wrap_future(future) for future in pending))

    async def _handle_response(
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
            )
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
            if inserted:
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
            await self._readiness.first_live_update()

    async def _mark_blocked(self) -> None:
        now = self._aware_now()
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
        if not self._subscribed or self._active_feed is None:
            return
        self._active_feed.unsubscribe_index_value(
            [self._wire_instrument(self._index, segment="CASH")]
        )
        derivative_wire = [
            self._wire_instrument(instrument, segment="FNO")
            for instrument in self._derivatives
        ]
        if derivative_wire:
            self._active_feed.unsubscribe_ltp(derivative_wire)
        self._subscribed = False

    async def _sleep_with_jitter(self, delay: float) -> None:
        jitter = self._jitter(delay)
        bounded = min(
            max(jitter, -delay * _MAX_JITTER_FRACTION),
            delay * _MAX_JITTER_FRACTION,
        )
        result = self._sleeper(delay + bounded)
        if inspect.isawaitable(result):
            await result

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
        material = f"{token}\0{observed_at.isoformat()}\0{price}"
        return f"feed:{sha256(material.encode()).hexdigest()}"


__all__ = ["GrowwFeed", "GrowwFeedWorker"]
