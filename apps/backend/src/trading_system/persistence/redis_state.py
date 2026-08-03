"""Typed, schema-versioned, disposable Redis live state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator
from redis.asyncio import Redis

from trading_system.persistence.postgres import PostgresRepository

NAMESPACE = "trading-system:v1:"
HEARTBEAT_TTL_SECONDS = 30
CANDLE_TTL_SECONDS = 600
QUOTE_TTL_SECONDS = 3600
OPTION_CHAIN_TTL_SECONDS = 3600
ACCOUNT_TTL_SECONDS = 300
# Subscriptions are replaceable process intent, not durable configuration. Expiry avoids a
# dead worker leaving apparently active subscriptions behind indefinitely.
SUBSCRIPTION_TTL_SECONDS = 3600

_FORBIDDEN_FIELDS = {
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "client_secret",
    "credential",
    "credentials",
    "sdk_client",
    "execution_state",
    "order",
    "orders",
    "fill",
    "fills",
}


def _assert_safe(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _FORBIDDEN_FIELDS:
                raise ValueError(f"forbidden live-state field: {key}")
            _assert_safe(child)
    elif isinstance(value, list):
        for child in value:
            _assert_safe(child)
    return value


class LiveState(BaseModel):
    """Common envelope guaranteeing an explicit codec version and UTC event time."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    observed_at: datetime
    payload: dict[str, JsonValue]

    @field_validator("observed_at", mode="after")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("payload", mode="after")
    @classmethod
    def safe_payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _assert_safe(value)
        return value


class QuoteState(LiveState):
    kind: Literal["quote"] = "quote"
    instrument_key: str


class OptionChainState(LiveState):
    kind: Literal["option_chain"] = "option_chain"
    chain_key: str


class InProgressCandleState(LiveState):
    kind: Literal["in_progress_candle"] = "in_progress_candle"
    instrument_key: str
    interval: str


class HeartbeatState(LiveState):
    kind: Literal["heartbeat"] = "heartbeat"
    service: str


class AccountSummaryState(LiveState):
    kind: Literal["account_summary"] = "account_summary"
    account_key: str


class SubscriptionState(LiveState):
    kind: Literal["subscription"] = "subscription"
    subscription_key: str


StateT = TypeVar("StateT", bound=LiveState)


class RedisLiveState:
    """Namespaced live cache; every value is safe to delete and rebuild."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def set_quote(self, instrument_key: str, value: QuoteState) -> None:
        await self._set("quote", instrument_key, value, QUOTE_TTL_SECONDS)

    async def get_quote(self, instrument_key: str) -> QuoteState | None:
        return await self._get("quote", instrument_key, QuoteState)

    async def set_option_chain(self, chain_key: str, value: OptionChainState) -> None:
        await self._set("option-chain", chain_key, value, OPTION_CHAIN_TTL_SECONDS)

    async def get_option_chain(self, chain_key: str) -> OptionChainState | None:
        return await self._get("option-chain", chain_key, OptionChainState)

    async def set_in_progress_candle(
        self, candle_key: str, value: InProgressCandleState
    ) -> None:
        await self._set("candle", candle_key, value, CANDLE_TTL_SECONDS)

    async def get_in_progress_candle(self, candle_key: str) -> InProgressCandleState | None:
        return await self._get("candle", candle_key, InProgressCandleState)

    async def set_heartbeat(self, service: str, value: HeartbeatState) -> None:
        await self._set("heartbeat", service, value, HEARTBEAT_TTL_SECONDS)

    async def get_heartbeat(self, service: str) -> HeartbeatState | None:
        return await self._get("heartbeat", service, HeartbeatState)

    async def set_account_summary(self, account_key: str, value: AccountSummaryState) -> None:
        await self._set("account-summary", account_key, value, ACCOUNT_TTL_SECONDS)

    async def get_account_summary(self, account_key: str) -> AccountSummaryState | None:
        return await self._get("account-summary", account_key, AccountSummaryState)

    async def set_subscription_state(
        self, subscription_key: str, value: SubscriptionState
    ) -> None:
        await self._set(
            "subscription", subscription_key, value, SUBSCRIPTION_TTL_SECONDS
        )

    async def get_subscription_state(self, subscription_key: str) -> SubscriptionState | None:
        return await self._get("subscription", subscription_key, SubscriptionState)

    async def rebuild_from_repository(self, repository: PostgresRepository) -> int:
        """Restore each identity's newest reconstructable state after cache loss."""
        rebuilt = 0
        seen: set[tuple[str, str]] = set()
        for observation in await repository.latest_observations():
            identity = ("quote", observation.instrument_key)
            if identity in seen:
                continue
            seen.add(identity)
            await self.set_quote(
                observation.instrument_key,
                QuoteState(
                    instrument_key=observation.instrument_key,
                    observed_at=observation.observed_at,
                    payload=observation.raw_payload,
                ),
            )
            rebuilt += 1
        for snapshot in await repository.latest_broker_snapshots("option_chain"):
            identity = ("option_chain", snapshot.snapshot_key)
            if identity in seen:
                continue
            seen.add(identity)
            await self.set_option_chain(
                snapshot.snapshot_key,
                OptionChainState(
                    chain_key=snapshot.snapshot_key,
                    observed_at=snapshot.observed_at,
                    payload=snapshot.raw_payload,
                ),
            )
            rebuilt += 1
        for account in await repository.latest_account_snapshots():
            identity = ("account", account.account_key)
            if identity in seen:
                continue
            seen.add(identity)
            await self.set_account_summary(
                account.account_key,
                AccountSummaryState(
                    account_key=account.account_key,
                    observed_at=account.observed_at,
                    payload=account.raw_payload,
                ),
            )
            rebuilt += 1
        for transition in await repository.latest_health_transitions():
            identity = ("heartbeat", transition.service)
            if identity in seen:
                continue
            seen.add(identity)
            await self.set_heartbeat(
                transition.service,
                HeartbeatState(
                    service=transition.service,
                    observed_at=transition.observed_at,
                    payload={"state": transition.state, "detail": transition.raw_payload},
                ),
            )
            rebuilt += 1
        for candle in await repository.latest_candles(finalized=False):
            candle_key = f"{candle.instrument_key}:{candle.interval}"
            identity = ("candle", candle_key)
            if identity in seen:
                continue
            seen.add(identity)
            await self.set_in_progress_candle(
                candle_key,
                InProgressCandleState(
                    instrument_key=candle.instrument_key,
                    interval=candle.interval,
                    observed_at=candle.opened_at,
                    payload=candle.raw_payload,
                ),
            )
            rebuilt += 1
        return rebuilt

    async def _set(
        self, category: str, identity: str, value: LiveState, ttl_seconds: int
    ) -> None:
        if not identity.strip():
            raise ValueError("Redis state identity must not be blank")
        await self._client.set(
            self._key(category, identity), value.model_dump_json(), ex=ttl_seconds
        )

    async def _get(
        self, category: str, identity: str, model: type[StateT]
    ) -> StateT | None:
        raw = await self._client.get(self._key(category, identity))
        if raw is None:
            return None
        return model.model_validate_json(raw)

    @staticmethod
    def _key(category: str, identity: str) -> str:
        return f"{NAMESPACE}{category}:{identity}"
