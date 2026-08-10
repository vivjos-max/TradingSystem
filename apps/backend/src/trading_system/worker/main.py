"""Worker-only startup ordering for authenticated live read orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from trading_system.brokers.ports import BrokerReadPort
from trading_system.services.snapshot import SnapshotBundle, SnapshotService


class StartupReadinessPort(Protocol):
    @property
    def is_ready(self) -> bool: ...

    async def dependencies_ready(self) -> None: ...

    async def reconciled(self) -> None: ...


class FeedWorkerPort(Protocol):
    async def run(self) -> None: ...


@dataclass(frozen=True, slots=True)
class StartupResult:
    """Successful reconciliation result retained by the worker process."""

    snapshot: SnapshotBundle
    readiness: StartupReadinessPort


class WorkerStartup:
    """Compose dependencies in the only process allowed to receive Groww secrets."""

    def __init__(
        self,
        *,
        settings_loader: Callable[[], Any],
        postgresql_starter: Callable[[Any], Awaitable[Any]],
        redis_starter: Callable[[Any], Awaitable[Any]],
        authenticator: Callable[[Any], BrokerReadPort],
        snapshot_factory: Callable[[BrokerReadPort], SnapshotService],
        readiness_factory: Callable[[Any], StartupReadinessPort],
        feed_worker_factory: Callable[
            [Any, Any, BrokerReadPort, SnapshotBundle, StartupReadinessPort],
            FeedWorkerPort,
        ],
    ) -> None:
        self._settings_loader = settings_loader
        self._postgresql_starter = postgresql_starter
        self._redis_starter = redis_starter
        self._authenticator = authenticator
        self._snapshot_factory = snapshot_factory
        self._readiness_factory = readiness_factory
        self._feed_worker_factory = feed_worker_factory

    async def run(self) -> StartupResult:
        settings = self._settings_loader()
        repository = await self._postgresql_starter(settings)
        redis_state = await self._redis_starter(settings)
        broker = self._authenticator(settings)
        readiness = self._readiness_factory(redis_state)
        await readiness.dependencies_ready()

        snapshot = await self._snapshot_factory(broker).refresh()
        await readiness.reconciled()

        feed_worker = self._feed_worker_factory(
            repository,
            redis_state,
            broker,
            snapshot,
            readiness,
        )
        await feed_worker.run()
        return StartupResult(snapshot=snapshot, readiness=readiness)


__all__ = ["StartupResult", "WorkerStartup"]
