"""Restart-safe readiness coordination through shared Redis live state."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import JsonValue

from trading_system.persistence.redis_state import HeartbeatState


class ReadinessStatePort(Protocol):
    """Only the approved Redis operations required for readiness."""

    async def set_heartbeat(self, service: str, value: HeartbeatState) -> None: ...

    async def get_heartbeat(self, service: str) -> HeartbeatState | None: ...


class ReadinessCoordinator:
    """Require all startup gates regardless of the order in which they arrive."""

    _SERVICE = "readiness"

    def __init__(
        self,
        redis_state: ReadinessStatePort,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._redis_state = redis_state
        self._clock = clock
        self._dependencies_ready = False
        self._reconciled = False
        self._first_live_update = False

    @property
    def is_ready(self) -> bool:
        return (
            self._dependencies_ready
            and self._reconciled
            and self._first_live_update
        )

    async def dependencies_ready(self) -> None:
        self._dependencies_ready = True
        await self._persist(state="ready" if self.is_ready else "starting")

    async def reconciled(self) -> None:
        self._reconciled = True
        await self._persist(state="ready" if self.is_ready else "starting")

    async def first_live_update(self) -> None:
        self._first_live_update = True
        await self._persist(state="ready" if self.is_ready else "starting")

    async def dependency_blocked(self, dependency: str) -> None:
        if not dependency.strip():
            raise ValueError("dependency must not be blank")
        self._dependencies_ready = False
        self._reconciled = False
        self._first_live_update = False
        await self._persist(state="blocked", blocked_dependency=dependency)

    async def restore(self) -> None:
        """Restore the gates for a separate process from the shared readiness value."""
        shared = await self._redis_state.get_heartbeat(self._SERVICE)
        if shared is None:
            return
        payload = shared.payload
        self._dependencies_ready = payload.get("dependencies_ready") is True
        self._reconciled = payload.get("reconciled") is True
        self._first_live_update = payload.get("first_live_update") is True

    async def _persist(
        self, *, state: str, blocked_dependency: str | None = None
    ) -> None:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        payload: dict[str, JsonValue] = {
            "dependencies_ready": self._dependencies_ready,
            "reconciled": self._reconciled,
            "first_live_update": self._first_live_update,
            "ready": self.is_ready,
            "state": state,
        }
        if blocked_dependency is not None:
            payload["blocked_dependency"] = blocked_dependency
        await self._redis_state.set_heartbeat(
            self._SERVICE,
            HeartbeatState(
                service=self._SERVICE,
                observed_at=observed_at,
                payload=payload,
            ),
        )


__all__ = ["ReadinessCoordinator", "ReadinessStatePort"]
