from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, SecretBytes, SecretStr
from redis import Redis
from redis.exceptions import RedisError

from .models import Candle, HealthState

_PREFIX = "trading-system:v1:"
_TTLS = {
    "quote": 3600,
    "option-chain": 3600,
    "candle": 600,
    "heartbeat": 30,
    "account": 300,
    "subscription": 30,
}
_SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "authtoken",
    "authorization",
    "clientsecret",
    "credential",
    "credentials",
    "growwtotpsecret",
    "growwtotptoken",
    "nkeyseed",
    "password",
    "privatekey",
    "postgrespassword",
    "redispassword",
    "refreshtoken",
    "sockettoken",
    "secret",
    "token",
    "totpsecret",
    "totptoken",
}


class StoreUnavailableError(RuntimeError):
    """A required storage dependency is unavailable, so readiness is BLOCKED."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes)):
        raise ValueError("secrets cannot be stored")
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            name = str(key)
            normalized_name = "".join(character for character in name.casefold() if character.isalnum())
            if normalized_name in _SENSITIVE_KEYS:
                raise ValueError(f"secret field cannot be stored: {name}")
            result[name] = _json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Enum):
        return value.value
    return value


class Store:
    def __init__(self, database_url: str, redis_url: str, *, timeout_seconds: int = 2) -> None:
        self._database_url = database_url
        self._redis = Redis.from_url(
            redis_url,
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
        )
        self._timeout_seconds = timeout_seconds

    @contextmanager
    def _postgres(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        try:
            with psycopg.connect(
                self._database_url,
                connect_timeout=self._timeout_seconds,
                options="-c timezone=UTC",
                row_factory=dict_row,
            ) as connection:
                yield connection
        except psycopg.OperationalError:
            raise StoreUnavailableError("BLOCKED: PostgreSQL is unavailable") from None

    def _redis_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self._redis, method)(*args, **kwargs)
        except RedisError:
            raise StoreUnavailableError("BLOCKED: Redis is unavailable") from None

    def healthcheck(self) -> None:
        with self._postgres() as connection:
            connection.execute("SELECT 1").fetchone()
        self._redis_call("ping")

    def append_event(
        self,
        *,
        kind: str,
        source: str,
        idempotency_key: str,
        event_at: datetime,
        received_at: datetime,
        raw: Mapping[str, Any],
        normalized: Mapping[str, Any],
        late: bool = False,
    ) -> bool:
        with self._postgres() as connection:
            row = connection.execute(
                """
                INSERT INTO events
                    (kind, source, idempotency_key, event_at, received_at, raw, normalized, late)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (
                    kind,
                    source,
                    idempotency_key,
                    _utc(event_at),
                    _utc(received_at),
                    Jsonb(_json_value(raw)),
                    Jsonb(_json_value(normalized)),
                    late,
                ),
            ).fetchone()
        return row is not None

    def read_events(
        self,
        *,
        kind: str | None = None,
        source: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses, values = [], []
        if kind is not None:
            clauses.append("kind = %s")
            values.append(kind)
        if source is not None:
            clauses.append("source = %s")
            values.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._postgres() as connection:
            rows = connection.execute(
                f"SELECT * FROM events {where} ORDER BY event_at DESC, received_at DESC, id DESC LIMIT %s",
                (*values, limit),
            ).fetchall()
        return rows

    def upsert_candle(self, candle: Candle) -> bool:
        with self._postgres() as connection:
            row = connection.execute(
                """
                INSERT INTO candles
                    (instrument, interval, bucket_start, open, high, low, close, volume,
                     source, finalized, observation_ids)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (instrument, interval, bucket_start, source) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    finalized = EXCLUDED.finalized,
                    observation_ids = EXCLUDED.observation_ids
                WHERE NOT candles.finalized AND
                    (candles.open, candles.high, candles.low, candles.close, candles.volume,
                     candles.finalized, candles.observation_ids)
                    IS DISTINCT FROM
                    (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume,
                     EXCLUDED.finalized, EXCLUDED.observation_ids)
                RETURNING id
                """,
                (
                    candle.instrument,
                    candle.interval,
                    _utc(candle.bucket_start),
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    str(candle.source),
                    candle.finalized,
                    Jsonb(list(candle.observation_ids)),
                ),
            ).fetchone()
        return row is not None

    def read_candles(
        self,
        instrument: str,
        interval: str,
        *,
        limit: int = 100,
    ) -> list[Candle]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._postgres() as connection:
            rows = connection.execute(
                """
                SELECT instrument, interval, bucket_start, open, high, low, close, volume,
                       source, finalized, observation_ids
                FROM candles
                WHERE instrument = %s AND interval = %s
                ORDER BY bucket_start DESC
                LIMIT %s
                """,
                (instrument, interval, limit),
            ).fetchall()
        return [Candle(**row) for row in reversed(rows)]

    def record_health_transition(
        self,
        *,
        subsystem: str,
        state: HealthState | str,
        observed_at: datetime,
        idempotency_key: str,
        reason: str | None = None,
    ) -> bool:
        with self._postgres() as connection:
            row = connection.execute(
                """
                INSERT INTO health_transitions
                    (subsystem, state, reason, observed_at, idempotency_key)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (subsystem, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (subsystem, str(state), reason, _utc(observed_at), idempotency_key),
            ).fetchone()
        return row is not None

    @staticmethod
    def _live_key(kind: str, identifier: str | None, interval: str | None) -> str:
        if kind not in _TTLS:
            raise ValueError(f"unknown live value kind: {kind}")
        if kind in {"quote", "heartbeat"}:
            if not identifier or interval is not None:
                raise ValueError(f"{kind} requires one identifier")
            suffix = f"{kind}:{identifier}"
        elif kind == "candle":
            if not identifier or interval not in {"1m", "5m"}:
                raise ValueError("candle requires an identifier and 1m or 5m interval")
            suffix = f"candle:{identifier}:{interval}"
        else:
            if identifier is not None or interval is not None:
                raise ValueError(f"{kind} does not accept an identifier or interval")
            suffix = kind
        return f"{_PREFIX}{suffix}"

    def set_live_value(
        self,
        kind: str,
        value: Any,
        *,
        identifier: str | None = None,
        interval: str | None = None,
    ) -> None:
        key = self._live_key(kind, identifier, interval)
        payload = json.dumps(_json_value(value), separators=(",", ":"), sort_keys=True)
        self._redis_call("setex", key, _TTLS[kind], payload)

    def get_live_value(
        self,
        kind: str,
        *,
        identifier: str | None = None,
        interval: str | None = None,
    ) -> Any | None:
        payload = self._redis_call("get", self._live_key(kind, identifier, interval))
        return None if payload is None else json.loads(payload)

    def rebuild_live_state(self) -> int:
        event_kinds = (
            "account_snapshot",
            "feed_event",
            "heartbeat",
            "market_observation",
            "normalized_observation",
            "option_chain",
            "subscription",
        )
        with self._postgres() as connection:
            events = connection.execute(
                """
                SELECT kind, normalized
                FROM (
                    SELECT kind, normalized, event_at, received_at, id,
                           row_number() OVER (
                               PARTITION BY kind,
                                   COALESCE(normalized->>'instrument', normalized->>'subsystem', '')
                               ORDER BY event_at DESC, received_at DESC, id DESC
                           ) AS latest
                    FROM events
                    WHERE kind = ANY(%s)
                ) candidates
                WHERE latest = 1
                ORDER BY event_at DESC, received_at DESC, id DESC
                """,
                (list(event_kinds),),
            ).fetchall()
            candles = connection.execute(
                """
                SELECT DISTINCT ON (instrument, interval)
                       instrument, interval, bucket_start, open, high, low, close, volume,
                       source, finalized, observation_ids
                FROM candles
                ORDER BY instrument, interval, bucket_start DESC
                """
            ).fetchall()

        rebuilt, seen = 0, set()
        for row in events:
            kind, value = row["kind"], row["normalized"]
            if kind in {"feed_event", "market_observation", "normalized_observation"}:
                live_kind, identifier = "quote", value.get("instrument")
            elif kind == "option_chain":
                live_kind, identifier = "option-chain", None
            elif kind == "account_snapshot":
                live_kind, identifier = "account", None
            elif kind == "subscription":
                live_kind, identifier = "subscription", None
            else:
                live_kind, identifier = "heartbeat", value.get("subsystem")
            if not identifier and live_kind in {"quote", "heartbeat"}:
                continue
            key = self._live_key(live_kind, identifier, None)
            if key in seen:
                continue
            self.set_live_value(live_kind, value, identifier=identifier)
            seen.add(key)
            rebuilt += 1

        for row in candles:
            candle = Candle(**row)
            self.set_live_value(
                "candle",
                candle,
                identifier=candle.instrument,
                interval=candle.interval,
            )
            rebuilt += 1
        return rebuilt

    def close(self) -> None:
        self._redis.close()
