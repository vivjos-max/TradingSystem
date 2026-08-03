from __future__ import annotations

import json

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
            readiness=_Ready(), repository=_UnusedRepository(), live_state=_UnusedLiveState()
        )
    )


def test_openapi_contains_no_mutation_or_execution_surface(app):
    schema = app.openapi()
    allowed = {"get", "head", "parameters"}
    for path, path_item in schema["paths"].items():
        assert set(path_item).issubset(allowed), path
        assert not any(
            word in path.lower()
            for word in ("place", "modify", "cancel", "order-ticket", "execute")
        )
    serialized = json.dumps(schema).lower()
    assert "placeorder" not in serialized
    assert "createorder" not in serialized
