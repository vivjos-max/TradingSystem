from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from trading_system.persistence.postgres import CandleInput, PostgresRepository
from trading_system.persistence.redis_state import (
    AccountSummaryState,
    HeartbeatState,
    InProgressCandleState,
    OptionChainState,
    QuoteState,
    RedisLiveState,
    SubscriptionState,
)
from trading_system.services.readiness import ReadinessCoordinator


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with (
        DockerContainer("redis:8-alpine")
        .with_exposed_ports(6379)
        .waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    ) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as postgres:
        url = postgres.get_connection_url(driver="asyncpg")
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        yield url


@pytest_asyncio.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def state(redis_client: Redis) -> AsyncIterator[RedisLiveState]:
    yield RedisLiveState(redis_client)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_typed_json_round_trips_with_exact_ttl_policies(
    state: RedisLiveState, redis_client: Redis
) -> None:
    at = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)
    await state.set_quote(
        "NSE:NIFTY50",
        QuoteState(instrument_key="NSE:NIFTY50", observed_at=at, payload={"last": "25001"}),
    )
    await state.set_option_chain(
        "NSE:NIFTY50:2026-08-06",
        OptionChainState(
            chain_key="NSE:NIFTY50:2026-08-06", observed_at=at, payload={"contracts": []}
        ),
    )
    await state.set_heartbeat(
        "groww", HeartbeatState(service="groww", observed_at=at, payload={"state": "healthy"})
    )
    await state.set_in_progress_candle(
        "NSE:NIFTY50:1m",
        InProgressCandleState(
            instrument_key="NSE:NIFTY50",
            interval="1m",
            observed_at=at,
            payload={"open": "25000", "close": "25001"},
        ),
    )
    await state.set_account_summary(
        "primary",
        AccountSummaryState(
            account_key="primary", observed_at=at, payload={"available_cash": "100000"}
        ),
    )
    await state.set_subscription_state(
        "market-feed",
        SubscriptionState(
            subscription_key="market-feed",
            observed_at=at,
            payload={"instruments": ["NSE:NIFTY50"]},
        ),
    )

    assert await state.get_quote("NSE:NIFTY50") == QuoteState(
        instrument_key="NSE:NIFTY50", observed_at=at, payload={"last": "25001"}
    )
    assert await state.get_heartbeat("groww") == HeartbeatState(
        service="groww", observed_at=at, payload={"state": "healthy"}
    )
    assert (await state.get_option_chain("NSE:NIFTY50:2026-08-06")).payload == {
        "contracts": []
    }
    assert (await state.get_in_progress_candle("NSE:NIFTY50:1m")).interval == "1m"
    assert (await state.get_account_summary("primary")).account_key == "primary"
    assert (await state.get_subscription_state("market-feed")).subscription_key == "market-feed"
    expected_ttls = {
        "trading-system:v1:quote:NSE:NIFTY50": 3600,
        "trading-system:v1:option-chain:NSE:NIFTY50:2026-08-06": 3600,
        "trading-system:v1:heartbeat:groww": 30,
        "trading-system:v1:candle:NSE:NIFTY50:1m": 600,
        "trading-system:v1:account-summary:primary": 300,
        "trading-system:v1:subscription:market-feed": 3600,
    }
    for key, expected in expected_ttls.items():
        assert expected - 2 <= await redis_client.ttl(key) <= expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_restores_replaceable_state_from_postgres(
    state: RedisLiveState, redis_client: Redis, postgres_url: str
) -> None:
    at = datetime(2026, 8, 3, 9, 16, tzinfo=UTC)
    engine = create_async_engine(postgres_url)
    repository = PostgresRepository(async_sessionmaker(engine, expire_on_commit=False))
    await repository.save_observation(
        instrument_key="NSE:NIFTY50",
        source="groww_live",
        idempotency_key="quote:rebuild:18",
        observed_at=at,
        raw_payload={"last": "25002"},
    )
    await repository.save_broker_snapshot(
        snapshot_kind="option_chain",
        snapshot_key="NSE:NIFTY50:2026-08-06",
        source="groww_live",
        idempotency_key="chain:rebuild:18",
        observed_at=at,
        raw_payload={"contracts": []},
    )
    await repository.save_account_snapshot(
        account_key="primary",
        source="groww_live",
        idempotency_key="account:rebuild:18",
        observed_at=at,
        raw_payload={"available_cash": "100001"},
    )
    await repository.save_health_transition(
        service="groww",
        state="healthy",
        source="backend",
        idempotency_key="health:rebuild:18",
        observed_at=at,
        raw_payload={"latency_ms": 10},
    )
    await repository.save_candles(
        source="groww_live",
        idempotency_key="candle:rebuild:18",
        candles=[
            CandleInput(
                instrument_key="NSE:NIFTY50",
                interval="1m",
                opened_at=at,
                closed_at=at + timedelta(minutes=1),
                open=Decimal("25000"),
                high=Decimal("25003"),
                low=Decimal("24999"),
                close=Decimal("25002"),
                volume=10,
                finalized=False,
                is_late=False,
                raw_payload={"open": "25000", "close": "25002"},
            )
        ],
    )
    await state.set_subscription_state(
        "market-feed",
        SubscriptionState(
            subscription_key="market-feed",
            observed_at=at,
            payload={"instruments": ["NSE:NIFTY50"]},
        ),
    )

    await redis_client.flushdb()
    rebuilt = await state.rebuild_from_repository(repository)
    await engine.dispose()

    assert rebuilt == 5
    assert (await state.get_quote("NSE:NIFTY50")).observed_at == at
    assert (await state.get_option_chain("NSE:NIFTY50:2026-08-06")).payload == {
        "contracts": []
    }
    assert (await state.get_account_summary("primary")).payload["available_cash"] == "100001"
    assert (await state.get_heartbeat("groww")).payload == {
        "detail": {"latency_ms": 10},
        "state": "healthy",
    }
    assert (await state.get_in_progress_candle("NSE:NIFTY50:1m")).payload == {
        "close": "25002",
        "open": "25000",
    }
    # Subscription intent deliberately has no durable source; a dead worker's intent expires
    # and a fresh worker must establish it again after reconnecting.
    assert await state.get_subscription_state("market-feed") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_existing_heartbeat_really_expires(
    state: RedisLiveState, redis_client: Redis
) -> None:
    key = "trading-system:v1:heartbeat:expiring"
    await state.set_heartbeat(
        "expiring",
        HeartbeatState(
            service="expiring",
            observed_at=datetime(2026, 8, 3, 9, 15, tzinfo=UTC),
            payload={"state": "healthy"},
        ),
    )
    assert 28 <= await redis_client.ttl(key) <= 30

    assert await redis_client.expire(key, 1)
    await asyncio.sleep(1.1)

    assert await state.get_heartbeat("expiring") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_recreation_starts_without_readiness_or_live_state() -> None:
    at = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)

    with (
        DockerContainer("redis:8-alpine")
        .with_exposed_ports(6379)
        .waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    ) as first_container:
        first_client: Redis = Redis.from_url(
            f"redis://{first_container.get_container_host_ip()}:"
            f"{first_container.get_exposed_port(6379)}/0",
            decode_responses=True,
        )
        first_state = RedisLiveState(first_client)
        first_readiness = ReadinessCoordinator(first_state, clock=lambda: at)
        await first_readiness.dependencies_ready()
        await first_readiness.reconciled()
        await first_readiness.first_live_update()
        await first_state.set_quote(
            "NIFTY50",
            QuoteState(
                instrument_key="NIFTY50",
                observed_at=at,
                payload={"last_price": "25001", "source": "groww_live"},
            ),
        )
        assert first_readiness.is_ready is True
        await first_client.aclose()

    with (
        DockerContainer("redis:8-alpine")
        .with_exposed_ports(6379)
        .waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    ) as replacement_container:
        replacement_client: Redis = Redis.from_url(
            f"redis://{replacement_container.get_container_host_ip()}:"
            f"{replacement_container.get_exposed_port(6379)}/0",
            decode_responses=True,
        )
        replacement_state = RedisLiveState(replacement_client)
        replacement_readiness = ReadinessCoordinator(
            replacement_state, clock=lambda: at
        )
        await replacement_readiness.restore()

        assert replacement_readiness.is_ready is False
        assert await replacement_state.get_quote("NIFTY50") is None
        assert await replacement_state.get_heartbeat("readiness") is None
        await replacement_client.aclose()
