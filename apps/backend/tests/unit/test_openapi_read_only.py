from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from trading_system.api.dependencies import AppContainer
from trading_system.api.main import create_app


class _Ready:
    @property
    def is_ready(self) -> bool:
        return True

    async def restore(self) -> None:
        return None


class _UnusedRepository:
    async def latest_health_transitions(self):
        return []

    async def latest_candles(self, *, finalized=None):
        return []

    async def recent_candles(self, *, instrument_key, interval, limit):
        return []

    async def latest_broker_snapshots(self, snapshot_kind):
        return []


class _UnusedLiveState:
    async def get_quote(self, instrument_key):
        return None

    async def get_option_chain(self, chain_key):
        return None

    async def get_in_progress_candle(self, candle_key):
        return None

    async def get_account_summary(self, account_key):
        return None


@pytest.fixture
def app():
    return create_app(
        AppContainer(
            readiness=_Ready(),
            repository=_UnusedRepository(),
            live_state=_UnusedLiveState(),
            nifty_future_instrument_key="TEST-NEAREST-FUTURE",
            clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
        )
    )


def test_openapi_contains_no_mutation_or_execution_surface(app):
    schema = app.openapi()
    assert set(schema["paths"]) == {
        "/health/live",
        "/health/ready",
        "/api/v1/system/status",
        "/api/v1/market/nifty",
        "/api/v1/market/nifty/options",
        "/api/v1/market/nifty/candles",
        "/api/v1/account/summary",
        "/api/v1/account/orders",
    }
    allowed = {"get", "head", "parameters"}
    for path, path_item in schema["paths"].items():
        assert set(path_item).issubset(allowed), path
        assert not any(
            word in path.lower()
            for word in (
                "place",
                "modify",
                "cancel",
                "order-ticket",
                "execute",
                "gtt",
                "oco",
                "trade",
                "buy",
                "sell",
                "submit",
            )
        )
    serialized = json.dumps(schema).lower()
    assert not any(
        word in serialized
        for word in (
            "placeorder",
            "createorder",
            "gtt",
            "oco",
            "trade",
            "buy",
            "sell",
            "submit",
        )
    )
