"""Deterministic system freshness states with explicit severity ordering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_system.domain.models import ServiceHealth, ServiceHealthState, SystemState, SystemStatus
from trading_system.market.session import SessionKind, SessionState
from trading_system.settings import Settings


def derive_system_status(
    session: SessionState,
    services: tuple[ServiceHealth, ...],
    now: datetime,
    thresholds: Settings,
) -> SystemStatus:
    """Derive state from injected time; no decision depends on the wall clock."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(UTC)
    last_observed_at = max((service.observed_at for service in services), default=None)

    if session.kind is SessionKind.BLOCKED or any(
        service.state is ServiceHealthState.UNAVAILABLE for service in services
    ):
        state = SystemState.BLOCKED
    elif session.kind is SessionKind.OPEN and any(
        now_utc - service.observed_at > timedelta(seconds=_stale_threshold_seconds(service, thresholds))
        for service in services
    ):
        state = SystemState.STALE
    elif any(service.state is ServiceHealthState.DEGRADED for service in services):
        state = SystemState.DEGRADED
    elif session.kind is SessionKind.CLOSED:
        state = SystemState.MARKET_CLOSED
    else:
        state = SystemState.LIVE

    return SystemStatus(
        observed_at=now_utc,
        services=services,
        state=state,
        last_observed_at=last_observed_at,
    )


def _stale_threshold_seconds(service: ServiceHealth, thresholds: Settings) -> int:
    if service.service == "quotes":
        return thresholds.quote_stale_seconds
    if service.service == "account":
        return thresholds.account_stale_seconds
    return thresholds.feed_stale_seconds


__all__ = ["SystemState", "derive_system_status"]
