from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from trading_system.persistence.postgres import CandleInput, PostgresRepository


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as postgres:
        url = postgres.get_connection_url(driver="asyncpg")
        backend_root = Path(__file__).parents[2]
        config = Config(str(backend_root / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        yield url


@pytest_asyncio.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    value = create_async_engine(postgres_url)
    try:
        yield value
    finally:
        await value.dispose()


@pytest_asyncio.fixture
async def repository(engine: AsyncEngine) -> AsyncIterator[PostgresRepository]:
    yield PostgresRepository(async_sessionmaker(engine, expire_on_commit=False))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_observation_is_idempotent_and_retains_utc_jsonb(
    repository: PostgresRepository,
) -> None:
    observed_at = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)
    payload = {"last_price": "25001.50", "provider": {"sequence": 17}}

    first = await repository.save_observation(
        instrument_key="NSE:NIFTY50",
        source="groww_live",
        idempotency_key="quote:NSE:NIFTY50:17",
        observed_at=observed_at,
        raw_payload=payload,
    )
    duplicate = await repository.save_observation(
        instrument_key="NSE:NIFTY50",
        source="groww_live",
        idempotency_key="quote:NSE:NIFTY50:17",
        observed_at=observed_at,
        raw_payload=payload,
    )

    rows = await repository.latest_observations()
    matching = [row for row in rows if row.idempotency_key == "quote:NSE:NIFTY50:17"]
    assert first is True
    assert duplicate is False
    assert len(matching) == 1
    assert matching[0].observed_at == observed_at
    assert matching[0].observed_at.utcoffset() == timedelta(0)
    assert matching[0].raw_payload == payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_late_observation_is_retained_as_audit_event(
    repository: PostgresRepository,
) -> None:
    await repository.save_observation(
        instrument_key="NSE:NIFTY50:LATE",
        source="groww_live",
        idempotency_key="quote:NSE:NIFTY50:late:16",
        observed_at=datetime(2026, 8, 3, 9, 14, 59, tzinfo=UTC),
        raw_payload={"last_price": "24999.00"},
        is_late=True,
    )

    rows = await repository.latest_observations(include_late=True)
    late = next(row for row in rows if row.idempotency_key.endswith(":16"))
    assert late.is_late is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_durable_writes_are_idempotent_and_readable(
    repository: PostgresRepository,
) -> None:
    at = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)
    assert await repository.save_instrument_import(
        source="groww",
        idempotency_key="instruments:2026-08-03",
        imported_at=at,
        instrument_count=1,
        raw_payload={"instruments": [{"token": "NIFTY50"}]},
    )
    assert await repository.save_broker_snapshot(
        snapshot_kind="option_chain",
        snapshot_key="NSE:NIFTY50:2026-08-06",
        source="groww_live",
        idempotency_key="chain:NIFTY50:17",
        observed_at=at,
        raw_payload={"contracts": []},
    )
    assert await repository.save_account_snapshot(
        account_key="primary",
        source="groww_live",
        idempotency_key="account:17",
        observed_at=at,
        raw_payload={"available_cash": "100000.00"},
    )
    assert await repository.save_health_transition(
        service="groww",
        state="healthy",
        source="backend",
        idempotency_key="health:groww:17",
        observed_at=at,
        raw_payload={"latency_ms": 12},
    )
    candle = CandleInput(
        instrument_key="NSE:NIFTY50",
        interval="1m",
        opened_at=at,
        closed_at=at + timedelta(minutes=1),
        open=Decimal("25000"),
        high=Decimal("25002"),
        low=Decimal("24999"),
        close=Decimal("25001"),
        volume=10,
        finalized=True,
        is_late=False,
        raw_payload={"provider_sequence": 17},
    )
    assert await repository.save_candles(
        source="groww_live", idempotency_key="candles:NIFTY50:09:15", candles=[candle]
    ) == 1
    assert await repository.save_candles(
        source="groww_live", idempotency_key="candles:NIFTY50:09:15", candles=[candle]
    ) == 0

    assert (await repository.latest_broker_snapshots("option_chain"))[0].raw_payload == {
        "contracts": []
    }
    assert (await repository.latest_account_snapshots())[0].account_key == "primary"
    assert (await repository.latest_health_transitions())[0].state == "healthy"
    assert (await repository.latest_candles())[0].close == Decimal("25001")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_rejects_naive_timestamp_before_write(
    repository: PostgresRepository,
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await repository.save_observation(
            instrument_key="NSE:NIFTY50",
            source="groww_live",
            idempotency_key="quote:naive",
            observed_at=datetime(2026, 8, 3, 9, 15),
            raw_payload={},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_reads_do_not_allow_one_busy_identity_to_starve_another(
    repository: PostgresRepository,
) -> None:
    base = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    await repository.save_broker_snapshot(
        snapshot_kind="option_chain",
        snapshot_key="sparse-chain",
        source="groww_live",
        idempotency_key="sparse-chain:0",
        observed_at=base,
        raw_payload={"sequence": 0},
    )
    for sequence in range(101):
        await repository.save_broker_snapshot(
            snapshot_kind="option_chain",
            snapshot_key="busy-chain",
            source="groww_live",
            idempotency_key=f"busy-chain:{sequence}",
            observed_at=base + timedelta(seconds=sequence + 1),
            raw_payload={"sequence": sequence},
        )

    latest = await repository.latest_broker_snapshots("option_chain")

    assert {row.snapshot_key for row in latest} >= {"busy-chain", "sparse-chain"}
    busy = next(row for row in latest if row.snapshot_key == "busy-chain")
    assert busy.raw_payload == {"sequence": 100}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_maximum_caller_candle_key_remains_bounded_and_idempotent(
    repository: PostgresRepository,
) -> None:
    at = datetime(2026, 8, 3, 10, 30, tzinfo=UTC)
    candle = CandleInput(
        instrument_key="NSE:NIFTY50",
        interval="5m",
        opened_at=at,
        closed_at=at + timedelta(minutes=5),
        open=Decimal("25000"),
        high=Decimal("25005"),
        low=Decimal("24995"),
        close=Decimal("25001"),
        volume=50,
        finalized=True,
        is_late=False,
        raw_payload={"source": "boundary"},
    )
    maximum_caller_key = "x" * 256

    assert await repository.save_candles(
        source="groww_live", idempotency_key=maximum_caller_key, candles=[candle]
    ) == 1
    assert await repository.save_candles(
        source="groww_live", idempotency_key=maximum_caller_key, candles=[candle]
    ) == 0
