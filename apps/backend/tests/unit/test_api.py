from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from trading_system.api import AppContainer, create_app
from trading_system.models import Candle
from trading_system.store import StoreUnavailableError

NOW = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self, ready=True):
        self.values = {}
        if ready:
            for subsystem in ("stores", "account_reconciliation", "subscriptions", "market", "feed"):
                self.values[("heartbeat", subsystem)] = {
                    "subsystem": subsystem,
                    "state": "HEALTHY",
                    "source_at": NOW.isoformat(),
                }
            self.values[("heartbeat", "market_session")] = {
                "subsystem": "market_session",
                "state": "HEALTHY",
                "reason": "MARKET_OPEN",
                "source_at": NOW.isoformat(),
            }
            self.values[("subscription", None)] = {
                "active": True,
                "spot": "NIFTY",
                "future": "NIFTY-FUT",
                "source": "LOCAL_RECONSTRUCTED",
                "source_at": NOW.isoformat(),
            }
            self.values[("quote", "NIFTY")] = {"price": "22100", "source": "GROWW_LIVE", "source_at": NOW.isoformat()}
            self.values[("quote", "NIFTY-FUT")] = {"price": "22150", "source": "GROWW_LIVE", "source_at": NOW.isoformat()}
            self.values[("option-chain", None)] = {"contracts": [], "source": "GROWW_LIVE", "source_at": NOW.isoformat()}
            self.values[("account", None)] = {
                "summary": {"available_margin": "1000", "positions": []},
                "orders": [],
                "source": "GROWW_LIVE",
                "source_at": NOW.isoformat(),
            }

    def get_live_value(self, kind, *, identifier=None, interval=None):
        return self.values.get((kind, identifier))

    def read_candles(self, instrument, interval, *, limit):
        return [Candle(
            instrument=instrument,
            interval=interval,
            bucket_start=NOW,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
        )]


def client(ready=True):
    return TestClient(create_app(AppContainer(MemoryStore(ready), lambda: NOW)))


def test_liveness_survives_blocked_readiness_and_error_is_safe() -> None:
    api = client(False)
    assert api.get("/health/live").status_code == 200
    response = api.get("/health/ready")
    assert response.status_code == 503
    assert set(response.json()) == {"code", "reason", "subsystem", "correlation_id", "last_success_at"}
    assert "secret" not in response.text.lower() and "token" not in response.text.lower()


def test_read_endpoints_return_only_normalized_source_timestamped_state() -> None:
    api = client()
    assert api.get("/health/ready").status_code == 200
    for path in (
        "/api/v1/market/nifty",
        "/api/v1/market/nifty/options",
        "/api/v1/market/nifty/candles?interval=1m",
        "/api/v1/account/summary",
        "/api/v1/account/orders",
    ):
        response = api.get(path)
        assert response.status_code == 200, path
        assert {"source", "source_at"} <= set(response.json()), path


def test_only_local_dashboard_origins_receive_cors_permission() -> None:
    api = client()
    allowed = api.get("/health/live", headers={"Origin": "http://localhost:3000"})
    denied = api.get("/health/live", headers={"Origin": "https://example.com"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in denied.headers


def test_invalid_candle_interval_is_rejected() -> None:
    assert client().get("/api/v1/market/nifty/candles?interval=1h").status_code == 422


def test_market_closed_is_not_misreported_as_stale_feed() -> None:
    store = MemoryStore()
    store.values.pop(("heartbeat", "feed"))
    store.values[("quote", "NIFTY")]["source_at"] = (NOW - timedelta(minutes=2)).isoformat()
    store.values[("heartbeat", "market_session")] = {
        "subsystem": "market_session",
        "state": "HEALTHY",
        "reason": "MARKET_CLOSED",
        "source_at": NOW.isoformat(),
    }
    response = TestClient(create_app(AppContainer(store, lambda: NOW))).get("/api/v1/system/status")
    assert response.status_code == 200
    assert response.json()["state"] == "MARKET_CLOSED"
    assert TestClient(create_app(AppContainer(store, lambda: NOW))).get("/api/v1/market/nifty").status_code == 200


def test_stale_account_is_unavailable_and_degraded_depth_is_global() -> None:
    store = MemoryStore()
    store.values[("account", None)]["source_at"] = (NOW - timedelta(minutes=2)).isoformat()
    api = TestClient(create_app(AppContainer(store, lambda: NOW)))
    response = api.get("/api/v1/account/summary")
    assert response.status_code == 503 and response.json()["reason"] == "ACCOUNT_STATE_STALE"

    store.values[("account", None)]["source_at"] = NOW.isoformat()
    store.values[("heartbeat", "depth")] = {"subsystem": "depth", "state": "DEGRADED", "reason": "MISSING_BOOK_SIDE"}
    assert api.get("/api/v1/system/status").json()["state"] == "DEGRADED"


def test_readiness_rejects_account_without_source_timestamp() -> None:
    store = MemoryStore()
    del store.values[("account", None)]["source_at"]
    response = TestClient(create_app(AppContainer(store, lambda: NOW))).get("/health/ready")
    assert response.status_code == 503


def test_stale_required_quote_cannot_report_live_or_serve_market_snapshot() -> None:
    store = MemoryStore()
    store.values[("quote", "NIFTY")]["source_at"] = (NOW - timedelta(minutes=2)).isoformat()
    api = TestClient(create_app(AppContainer(store, lambda: NOW)))
    assert api.get("/api/v1/system/status").json()["state"] == "STALE"
    response = api.get("/api/v1/market/nifty")
    assert response.status_code == 503 and response.json()["reason"] == "QUOTE_STALE"

    store.values[("quote", "NIFTY")]["source_at"] = (NOW + timedelta(minutes=2)).isoformat()
    assert api.get("/api/v1/system/status").json()["state"] == "BLOCKED"
    response = api.get("/api/v1/market/nifty")
    assert response.status_code == 503 and response.json()["reason"] == "QUOTE_TIMESTAMP_FUTURE"


def test_dependency_health_failure_overrides_cached_healthy_heartbeats() -> None:
    class FailedStore(MemoryStore):
        def healthcheck(self):
            raise StoreUnavailableError("down")

    store = FailedStore()
    store.values[("heartbeat", "market_session")]["reason"] = "MARKET_CLOSED"
    response = TestClient(create_app(AppContainer(store, lambda: NOW))).get("/api/v1/system/status")
    assert response.json()["state"] == "BLOCKED"
