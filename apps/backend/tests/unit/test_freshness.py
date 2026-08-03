from datetime import UTC, datetime, timedelta

from trading_system.domain.models import ServiceHealth, ServiceHealthState
from trading_system.market.freshness import SystemState, derive_system_status
from trading_system.market.session import SessionKind, SessionState
from trading_system.settings import Settings


def service(state: ServiceHealthState, seen_at: datetime) -> ServiceHealth:
    return ServiceHealth(service="quotes", state=state, observed_at=seen_at)


def open_session() -> SessionState:
    return SessionState(
        kind=SessionKind.OPEN,
        opened_at=datetime(2026, 8, 3, 3, 45, tzinfo=UTC),
        closed_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
    )


def test_status_precedence_is_blocked_stale_degraded_market_closed_live():
    now = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)
    thresholds = Settings(_env_file=None)
    healthy = service(ServiceHealthState.HEALTHY, now)
    degraded = service(ServiceHealthState.DEGRADED, now)
    stale = service(ServiceHealthState.HEALTHY, now - timedelta(seconds=16))
    closed = SessionState(kind=SessionKind.CLOSED, reason="weekend")
    blocked = SessionState(kind=SessionKind.BLOCKED, reason="calendar-unknown")

    assert derive_system_status(open_session(), (healthy,), now, thresholds).state is SystemState.LIVE
    assert derive_system_status(closed, (healthy,), now, thresholds).state is SystemState.MARKET_CLOSED
    assert derive_system_status(open_session(), (degraded,), now, thresholds).state is SystemState.DEGRADED
    assert derive_system_status(open_session(), (stale,), now, thresholds).state is SystemState.STALE
    assert derive_system_status(blocked, (healthy,), now, thresholds).state is SystemState.BLOCKED
    assert derive_system_status(open_session(), (stale, degraded), now, thresholds).state is SystemState.STALE
    assert derive_system_status(blocked, (stale, degraded), now, thresholds).state is SystemState.BLOCKED


def test_closed_market_keeps_last_observation_timestamp_without_becoming_stale():
    now = datetime(2026, 8, 2, 4, 0, tzinfo=UTC)
    last_seen = now - timedelta(days=2)
    closed = SessionState(kind=SessionKind.CLOSED, reason="weekend")

    status = derive_system_status(
        closed, (service(ServiceHealthState.HEALTHY, last_seen),), now, Settings(_env_file=None)
    )

    assert status.state is SystemState.MARKET_CLOSED
    assert status.last_observed_at == last_seen
