from datetime import datetime, timezone

from trading_system.worker import ReadinessCoordinator


class MemoryStore:
    def __init__(self) -> None:
        self.values = {}

    def set_live_value(self, kind, value, *, identifier=None, interval=None):
        self.values[(kind, identifier, interval)] = value


def test_readiness_requires_every_ordered_startup_gate() -> None:
    store = MemoryStore()
    ready = ReadinessCoordinator(store, lambda: datetime(2026, 8, 10, tzinfo=timezone.utc))

    ready.dependencies_ready()
    ready.first_live_update()
    ready.subscriptions_active()
    assert not ready.is_ready

    ready.account_reconciled()
    assert ready.is_ready
    ready.blocked("feed", "FEED_STALE")
    assert not ready.is_ready
