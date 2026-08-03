"""Explicit async PostgreSQL repository for immutable read-only observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_system.persistence.models import (
    AccountSnapshotRow,
    BrokerSnapshotRow,
    CandleRow,
    HealthTransitionRow,
    InstrumentImportRow,
    MarketObservationRow,
)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _required(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value


@dataclass(frozen=True, slots=True)
class CandleInput:
    instrument_key: str
    interval: str
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None
    finalized: bool
    is_late: bool
    raw_payload: dict[str, Any]

    def __post_init__(self) -> None:
        _required(self.instrument_key, "instrument_key")
        _required(self.interval, "interval")
        object.__setattr__(self, "opened_at", _utc(self.opened_at, "opened_at"))
        object.__setattr__(self, "closed_at", _utc(self.closed_at, "closed_at"))
        if self.closed_at <= self.opened_at:
            raise ValueError("closed_at must be later than opened_at")


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    instrument_key: str
    source: str
    idempotency_key: str
    observed_at: datetime
    is_late: bool
    raw_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BrokerSnapshotRecord:
    snapshot_kind: str
    snapshot_key: str
    source: str
    idempotency_key: str
    observed_at: datetime
    raw_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AccountSnapshotRecord:
    account_key: str
    source: str
    idempotency_key: str
    observed_at: datetime
    raw_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HealthTransitionRecord:
    service: str
    state: str
    source: str
    idempotency_key: str
    observed_at: datetime
    raw_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CandleRecord:
    instrument_key: str
    interval: str
    source: str
    idempotency_key: str
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None
    finalized: bool
    is_late: bool
    raw_payload: dict[str, Any]


class PostgresRepository:
    """Narrow repository; it deliberately exposes no generic SQL execution surface."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save_instrument_import(
        self,
        *,
        source: str,
        idempotency_key: str,
        imported_at: datetime,
        instrument_count: int,
        raw_payload: dict[str, Any],
    ) -> bool:
        if instrument_count < 0:
            raise ValueError("instrument_count must be non-negative")
        return await self._insert_immutable(
            InstrumentImportRow,
            source=_required(source, "source"),
            idempotency_key=_required(idempotency_key, "idempotency_key"),
            imported_at=_utc(imported_at, "imported_at"),
            instrument_count=instrument_count,
            raw_payload=raw_payload,
        )

    async def save_broker_snapshot(
        self,
        *,
        snapshot_kind: str,
        snapshot_key: str,
        source: str,
        idempotency_key: str,
        observed_at: datetime,
        raw_payload: dict[str, Any],
    ) -> bool:
        return await self._insert_immutable(
            BrokerSnapshotRow,
            snapshot_kind=_required(snapshot_kind, "snapshot_kind"),
            snapshot_key=_required(snapshot_key, "snapshot_key"),
            source=_required(source, "source"),
            idempotency_key=_required(idempotency_key, "idempotency_key"),
            observed_at=_utc(observed_at, "observed_at"),
            raw_payload=raw_payload,
        )

    async def save_observation(
        self,
        *,
        instrument_key: str,
        source: str,
        idempotency_key: str,
        observed_at: datetime,
        raw_payload: dict[str, Any],
        is_late: bool = False,
    ) -> bool:
        return await self._insert_immutable(
            MarketObservationRow,
            instrument_key=_required(instrument_key, "instrument_key"),
            source=_required(source, "source"),
            idempotency_key=_required(idempotency_key, "idempotency_key"),
            observed_at=_utc(observed_at, "observed_at"),
            raw_payload=raw_payload,
            is_late=is_late,
        )

    async def save_candles(
        self,
        *,
        source: str,
        idempotency_key: str,
        candles: list[CandleInput] | tuple[CandleInput, ...],
    ) -> int:
        clean_source = _required(source, "source")
        batch_key = _required(idempotency_key, "idempotency_key")
        inserted = 0
        for candle in candles:
            natural_key = (
                f"{candle.instrument_key}|{candle.interval}|{candle.opened_at.isoformat()}|"
                f"{clean_source}"
            )
            row_key = f"{batch_key}:{sha256(natural_key.encode()).hexdigest()}"
            inserted += await self._insert_immutable(
                CandleRow,
                instrument_key=candle.instrument_key,
                interval=candle.interval,
                source=clean_source,
                idempotency_key=row_key,
                opened_at=candle.opened_at,
                closed_at=candle.closed_at,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                finalized=candle.finalized,
                is_late=candle.is_late,
                raw_payload=candle.raw_payload,
            )
        return inserted

    async def save_account_snapshot(
        self,
        *,
        account_key: str,
        source: str,
        idempotency_key: str,
        observed_at: datetime,
        raw_payload: dict[str, Any],
    ) -> bool:
        return await self._insert_immutable(
            AccountSnapshotRow,
            account_key=_required(account_key, "account_key"),
            source=_required(source, "source"),
            idempotency_key=_required(idempotency_key, "idempotency_key"),
            observed_at=_utc(observed_at, "observed_at"),
            raw_payload=raw_payload,
        )

    async def save_health_transition(
        self,
        *,
        service: str,
        state: str,
        source: str,
        idempotency_key: str,
        observed_at: datetime,
        raw_payload: dict[str, Any],
    ) -> bool:
        return await self._insert_immutable(
            HealthTransitionRow,
            service=_required(service, "service"),
            state=_required(state, "state"),
            source=_required(source, "source"),
            idempotency_key=_required(idempotency_key, "idempotency_key"),
            observed_at=_utc(observed_at, "observed_at"),
            raw_payload=raw_payload,
        )

    async def latest_observations(
        self, *, include_late: bool = False, limit: int = 500
    ) -> list[ObservationRecord]:
        statement = select(MarketObservationRow)
        if not include_late:
            statement = statement.where(MarketObservationRow.is_late.is_(False))
        statement = statement.order_by(MarketObservationRow.observed_at.desc()).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        return [
            ObservationRecord(
                row.instrument_key,
                row.source,
                row.idempotency_key,
                _utc(row.observed_at, "observed_at"),
                row.is_late,
                row.raw_payload,
            )
            for row in rows
        ]

    async def latest_broker_snapshots(
        self, snapshot_kind: str, *, limit: int = 100
    ) -> list[BrokerSnapshotRecord]:
        statement = (
            select(BrokerSnapshotRow)
            .where(BrokerSnapshotRow.snapshot_kind == snapshot_kind)
            .order_by(BrokerSnapshotRow.observed_at.desc())
            .limit(limit)
        )
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        return [
            BrokerSnapshotRecord(
                row.snapshot_kind,
                row.snapshot_key,
                row.source,
                row.idempotency_key,
                _utc(row.observed_at, "observed_at"),
                row.raw_payload,
            )
            for row in rows
        ]

    async def latest_account_snapshots(self, *, limit: int = 100) -> list[AccountSnapshotRecord]:
        statement = select(AccountSnapshotRow).order_by(
            AccountSnapshotRow.observed_at.desc()
        ).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        return [
            AccountSnapshotRecord(
                row.account_key,
                row.source,
                row.idempotency_key,
                _utc(row.observed_at, "observed_at"),
                row.raw_payload,
            )
            for row in rows
        ]

    async def latest_health_transitions(
        self, *, limit: int = 100
    ) -> list[HealthTransitionRecord]:
        statement = select(HealthTransitionRow).order_by(
            HealthTransitionRow.observed_at.desc()
        ).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        return [
            HealthTransitionRecord(
                row.service,
                row.state,
                row.source,
                row.idempotency_key,
                _utc(row.observed_at, "observed_at"),
                row.raw_payload,
            )
            for row in rows
        ]

    async def latest_candles(
        self, *, finalized: bool | None = None, limit: int = 500
    ) -> list[CandleRecord]:
        statement = select(CandleRow)
        if finalized is not None:
            statement = statement.where(CandleRow.finalized.is_(finalized))
        statement = statement.order_by(CandleRow.opened_at.desc()).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        return [
            CandleRecord(
                row.instrument_key,
                row.interval,
                row.source,
                row.idempotency_key,
                _utc(row.opened_at, "opened_at"),
                _utc(row.closed_at, "closed_at"),
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
                row.finalized,
                row.is_late,
                row.raw_payload,
            )
            for row in rows
        ]

    async def _insert_immutable(self, model: type[Any], **values: Any) -> bool:
        statement = insert(model).values(**values).on_conflict_do_nothing().returning(model.id)
        async with self._sessions.begin() as session:
            result = await session.execute(statement)
        return result.scalar_one_or_none() is not None
