"""SQLAlchemy mappings for the read-only foundation's durable audit data."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DurableEvent:
    """Columns shared by immutable, source-idempotent durable events."""

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InstrumentImportRow(DurableEvent, Base):
    __tablename__ = "instrument_imports"
    __table_args__ = (UniqueConstraint("source", "idempotency_key"),)

    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    instrument_count: Mapped[int] = mapped_column(Integer, nullable=False)


class BrokerSnapshotRow(DurableEvent, Base):
    __tablename__ = "broker_snapshots"
    __table_args__ = (UniqueConstraint("source", "idempotency_key"),)

    snapshot_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketObservationRow(DurableEvent, Base):
    __tablename__ = "market_observations"
    __table_args__ = (UniqueConstraint("source", "idempotency_key"),)

    instrument_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CandleRow(DurableEvent, Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("source", "idempotency_key"),
        UniqueConstraint("instrument_key", "interval", "opened_at", "source"),
    )

    instrument_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    finalized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AccountSnapshotRow(DurableEvent, Base):
    __tablename__ = "account_snapshots"
    __table_args__ = (UniqueConstraint("source", "idempotency_key"),)

    account_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HealthTransitionRow(DurableEvent, Base):
    __tablename__ = "health_transitions"
    __table_args__ = (UniqueConstraint("source", "idempotency_key"),)

    service: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
