"""Create durable read-only market and account tables.

Revision ID: 0001_read_only_foundation
Revises:
Create Date: 2026-08-03
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_read_only_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _durable_columns() -> list[Any]:
    return [
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    ]


def upgrade() -> None:
    op.create_table(
        "instrument_imports",
        *_durable_columns(),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("source", "idempotency_key"),
    )
    op.create_table(
        "broker_snapshots",
        *_durable_columns(),
        sa.Column("snapshot_kind", sa.String(length=64), nullable=False),
        sa.Column("snapshot_key", sa.String(length=256), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "idempotency_key"),
    )
    op.create_index(
        op.f("ix_broker_snapshots_snapshot_kind"),
        "broker_snapshots",
        ["snapshot_kind"],
        unique=False,
    )
    op.create_index(
        op.f("ix_broker_snapshots_snapshot_key"),
        "broker_snapshots",
        ["snapshot_key"],
        unique=False,
    )
    op.create_table(
        "market_observations",
        *_durable_columns(),
        sa.Column("instrument_key", sa.String(length=256), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_late", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("source", "idempotency_key"),
    )
    op.create_index(
        op.f("ix_market_observations_instrument_key"),
        "market_observations",
        ["instrument_key"],
        unique=False,
    )
    op.create_table(
        "candles",
        *_durable_columns(),
        sa.Column("instrument_key", sa.String(length=256), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("finalized", sa.Boolean(), nullable=False),
        sa.Column("is_late", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("instrument_key", "interval", "opened_at", "source"),
        sa.UniqueConstraint("source", "idempotency_key"),
    )
    op.create_index(
        op.f("ix_candles_instrument_key"), "candles", ["instrument_key"], unique=False
    )
    op.create_table(
        "account_snapshots",
        *_durable_columns(),
        sa.Column("account_key", sa.String(length=128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "idempotency_key"),
    )
    op.create_index(
        op.f("ix_account_snapshots_account_key"),
        "account_snapshots",
        ["account_key"],
        unique=False,
    )
    op.create_table(
        "health_transitions",
        *_durable_columns(),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "idempotency_key"),
    )
    op.create_index(
        op.f("ix_health_transitions_service"),
        "health_transitions",
        ["service"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_health_transitions_service"), table_name="health_transitions")
    op.drop_table("health_transitions")
    op.drop_index(op.f("ix_account_snapshots_account_key"), table_name="account_snapshots")
    op.drop_table("account_snapshots")
    op.drop_index(op.f("ix_candles_instrument_key"), table_name="candles")
    op.drop_table("candles")
    op.drop_index(op.f("ix_market_observations_instrument_key"), table_name="market_observations")
    op.drop_table("market_observations")
    op.drop_index(op.f("ix_broker_snapshots_snapshot_key"), table_name="broker_snapshots")
    op.drop_index(op.f("ix_broker_snapshots_snapshot_kind"), table_name="broker_snapshots")
    op.drop_table("broker_snapshots")
    op.drop_table("instrument_imports")
