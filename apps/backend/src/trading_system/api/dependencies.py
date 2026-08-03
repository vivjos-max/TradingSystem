"""Narrow, read-only dependencies admitted to the dashboard API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from trading_system.persistence.postgres import (
    BrokerSnapshotRecord,
    CandleRecord,
    HealthTransitionRecord,
)
from trading_system.persistence.redis_state import (
    AccountSummaryState,
    InProgressCandleState,
    OptionChainState,
    QuoteState,
)


class ReadinessPort(Protocol):
    """Read-only readiness view shared by the worker and API processes."""

    @property
    def is_ready(self) -> bool: ...

    async def restore(self) -> None: ...


class LiveDashboardStatePort(Protocol):
    """Only typed live values consumed by the browser dashboard."""

    async def get_quote(self, instrument_key: str) -> QuoteState | None: ...

    async def get_option_chain(self, chain_key: str) -> OptionChainState | None: ...

    async def get_in_progress_candle(
        self, candle_key: str
    ) -> InProgressCandleState | None: ...

    async def get_account_summary(self, account_key: str) -> AccountSummaryState | None: ...


class DashboardRepositoryPort(Protocol):
    """The persisted record reads required by the read-only dashboard."""

    async def latest_health_transitions(self) -> list[HealthTransitionRecord]: ...

    async def latest_candles(self, *, finalized: bool | None = None) -> list[CandleRecord]: ...

    async def recent_candles(
        self, instrument_key: str, interval: str, *, limit: int
    ) -> list[CandleRecord]: ...

    async def latest_broker_snapshots(
        self, snapshot_kind: str
    ) -> list[BrokerSnapshotRecord]: ...


@dataclass(frozen=True, slots=True)
class AppContainer:
    """Explicit API composition root with no settings, SDK, or execution capability."""

    readiness: ReadinessPort
    repository: DashboardRepositoryPort
    live_state: LiveDashboardStatePort
    nifty_instrument_key: str = "NIFTY50"
    nifty_future_instrument_key: str = ""
    option_chain_key: str = "nifty-current"
    account_key: str = "primary"
    option_strikes_each_side: int = 5
    candle_history_limit: int = 100
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


__all__ = [
    "AppContainer",
    "DashboardRepositoryPort",
    "LiveDashboardStatePort",
    "ReadinessPort",
]
