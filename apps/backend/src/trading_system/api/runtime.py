"""Concrete API composition containing only read-side infrastructure."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_system.api.dependencies import AppContainer
from trading_system.api.main import create_app
from trading_system.persistence.postgres import PostgresRepository
from trading_system.persistence.redis_state import RedisLiveState
from trading_system.services.readiness import ReadinessCoordinator
from trading_system.settings import Settings, get_settings
from trading_system.worker.runtime import NEAREST_FUTURE_QUOTE_KEY


def build_runtime_app(settings: Settings | None = None) -> FastAPI:
    """Build the localhost API without importing broker or execution capabilities."""
    configured = settings or get_settings()
    engine = create_async_engine(configured.database_url, pool_pre_ping=True)
    repository = PostgresRepository(async_sessionmaker(engine, expire_on_commit=False))
    redis_client = Redis.from_url(configured.redis_url, decode_responses=True)
    live_state = RedisLiveState(redis_client)
    readiness = ReadinessCoordinator(live_state)
    container = AppContainer(
        readiness=readiness,
        repository=repository,
        live_state=live_state,
        nifty_future_instrument_key=NEAREST_FUTURE_QUOTE_KEY,
        clock=lambda: datetime.now(UTC),
        cors_origins=configured.cors_origins,
    )
    return create_app(container)


app = build_runtime_app()


__all__ = ["app", "build_runtime_app"]
