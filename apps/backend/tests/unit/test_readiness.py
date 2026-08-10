from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_system.persistence.redis_state import HeartbeatState
from trading_system.services.readiness import ReadinessCoordinator


AT = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)


class MemoryRedisState:
    """Complete readiness storage boundary used by the coordinator."""

    def __init__(self) -> None:
        self.heartbeats: dict[str, HeartbeatState] = {}

    async def set_heartbeat(self, service: str, value: HeartbeatState) -> None:
        self.heartbeats[service] = value

    async def get_heartbeat(self, service: str) -> HeartbeatState | None:
        return self.heartbeats.get(service)


@pytest.fixture
def redis_state() -> MemoryRedisState:
    return MemoryRedisState()


@pytest.fixture
def coordinator(redis_state: MemoryRedisState) -> ReadinessCoordinator:
    return ReadinessCoordinator(redis_state, clock=lambda: AT)


@pytest.mark.asyncio
async def test_readiness_requires_reconciliation_before_first_tick(
    coordinator: ReadinessCoordinator,
) -> None:
    await coordinator.dependencies_ready()
    await coordinator.first_live_update()
    assert coordinator.is_ready is False
    await coordinator.reconciled()
    assert coordinator.is_ready is True


@pytest.mark.asyncio
async def test_blocked_dependency_resets_ready_state_in_shared_redis(
    coordinator: ReadinessCoordinator, redis_state: MemoryRedisState
) -> None:
    await coordinator.dependencies_ready()
    await coordinator.reconciled()
    await coordinator.first_live_update()

    await coordinator.dependency_blocked("postgresql")

    assert coordinator.is_ready is False
    shared = await redis_state.get_heartbeat("readiness")
    assert shared == HeartbeatState(
        service="readiness",
        observed_at=AT,
        payload={
            "dependencies_ready": False,
            "reconciled": False,
            "first_live_update": False,
            "ready": False,
            "state": "blocked",
            "blocked_dependency": "postgresql",
        },
    )


@pytest.mark.asyncio
async def test_separate_process_can_restore_readiness_from_shared_redis(
    coordinator: ReadinessCoordinator, redis_state: MemoryRedisState
) -> None:
    await coordinator.dependencies_ready()
    await coordinator.reconciled()
    await coordinator.first_live_update()

    api_coordinator = ReadinessCoordinator(redis_state, clock=lambda: AT)
    await api_coordinator.restore()

    assert api_coordinator.is_ready is True
