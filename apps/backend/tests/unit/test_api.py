from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from trading_system.api.dependencies import AppContainer
from trading_system.api.main import create_app
from trading_system.persistence.postgres import (
    BrokerSnapshotRecord,
    CandleRecord,
    HealthTransitionRecord,
)
from trading_system.persistence.redis_state import (
    AccountSummaryState,
    OptionChainState,
    QuoteState,
)


AT = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)


class DashboardState:
    async def get_quote(self, instrument_key: str) -> QuoteState | None:
        assert instrument_key == "NIFTY50"
        return QuoteState(
            instrument_key=instrument_key,
            observed_at=AT,
            payload={
                "last_price": "25001.50",
                "source": "groww_live",
                "raw": {"provider_symbol": "NIFTY"},
            },
        )

    async def get_option_chain(self, chain_key: str) -> OptionChainState | None:
        assert chain_key == "nifty-current"
        return OptionChainState(
            chain_key=chain_key,
            observed_at=AT,
            payload={
                "expiry": "2026-08-06",
                "source": "groww_live",
                "contracts": [
                    {
                        "instrument_token": "NIFTY26AUG25000CE",
                        "strike": "25000",
                        "option_type": "CE",
                        "last_price": "120.50",
                    }
                ],
                "raw": {"provider_symbol": "NIFTY"},
            },
        )

    async def get_account_summary(self, account_key: str) -> AccountSummaryState | None:
        assert account_key == "primary"
        return AccountSummaryState(
            account_key=account_key,
            observed_at=AT,
            payload={
                "available_cash": "100000",
                "used_margin": "25000",
                "collateral": "1000",
                "source": "groww_live",
                "raw": {"provider_account_id": "internal-only"},
            },
        )


class DashboardRepository:
    async def latest_health_transitions(self) -> list[HealthTransitionRecord]:
        return [
            HealthTransitionRecord(
                service="groww-feed",
                state="live",
                source="worker",
                idempotency_key="ignored",
                observed_at=AT,
                raw_payload={"access_token": "must-not-leak"},
            )
        ]

    async def latest_candles(self, *, finalized: bool | None = None) -> list[CandleRecord]:
        assert finalized is True
        return [
            CandleRecord(
                instrument_key="NIFTY50",
                interval="1m",
                source="groww_live",
                idempotency_key="ignored",
                opened_at=AT,
                closed_at=datetime(2026, 8, 3, 9, 16, tzinfo=UTC),
                open=Decimal("25000"),
                high=Decimal("25005"),
                low=Decimal("24999"),
                close=Decimal("25001.50"),
                volume=100,
                finalized=True,
                is_late=False,
                raw_payload={"access_token": "must-not-leak"},
            )
        ]

    async def latest_broker_snapshots(self, snapshot_kind: str) -> list[BrokerSnapshotRecord]:
        assert snapshot_kind == "orders"
        return [
            BrokerSnapshotRecord(
                snapshot_kind="orders",
                snapshot_key="primary",
                source="groww_live",
                idempotency_key="ignored",
                observed_at=AT,
                raw_payload={
                    "orders": [
                        {
                            "order_id": "ord-1",
                            "instrument_token": "NIFTY26AUG25000CE",
                            "side": "BUY",
                            "quantity": 75,
                            "status": "COMPLETE",
                            "created_at": "2026-08-03T09:14:00Z",
                            "updated_at": "2026-08-03T09:15:00Z",
                            "access_token": "must-not-leak",
                        }
                    ]
                },
            )
        ]


class NotReady:
    @property
    def is_ready(self) -> bool:
        return False

    async def restore(self) -> None:
        return None


class Ready(NotReady):
    @property
    def is_ready(self) -> bool:
        return True


@pytest.fixture
def app():
    return create_app(
        AppContainer(
            readiness=Ready(),
            repository=DashboardRepository(),
            live_state=DashboardState(),
            option_chain_key="nifty-current",
        )
    )


@pytest.mark.asyncio
async def test_live_is_available_when_readiness_dependencies_are_blocked() -> None:
    app = create_app(
        AppContainer(
            readiness=NotReady(), repository=DashboardRepository(), live_state=DashboardState()
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "live"
    assert response.json()["correlation_id"]
    assert response.json()["observed_at"].endswith("Z")


@pytest.mark.asyncio
async def test_ready_returns_safe_503_until_the_coordinator_is_ready() -> None:
    app = create_app(
        AppContainer(
            readiness=NotReady(), repository=DashboardRepository(), live_state=DashboardState()
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "LIVE_STATE_UNAVAILABLE"
    assert response.json()["correlation_id"]
    assert "blocked" not in response.text.lower()


@pytest.mark.asyncio
async def test_api_startup_restores_shared_readiness_before_serving() -> None:
    class RestoringReadiness(NotReady):
        def __init__(self) -> None:
            self.restored = False

        @property
        def is_ready(self) -> bool:
            return self.restored

        async def restore(self) -> None:
            self.restored = True

    readiness = RestoringReadiness()
    app = create_app(
        AppContainer(
            readiness=readiness, repository=DashboardRepository(), live_state=DashboardState()
        )
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/health/ready")

    assert response.status_code == 200
    assert readiness.restored is True


@pytest.mark.asyncio
async def test_failed_readiness_restore_stays_a_safe_unavailable_response() -> None:
    class FailingReadiness(NotReady):
        async def restore(self) -> None:
            raise RuntimeError("redis password=must-not-leak")

    app = create_app(
        AppContainer(
            readiness=FailingReadiness(), repository=DashboardRepository(), live_state=DashboardState()
        )
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/health/ready")

    assert response.status_code == 503
    assert "password" not in response.text.lower()


@pytest.mark.asyncio
async def test_market_and_account_routes_return_normalized_models_with_source_timestamps(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        nifty = await client.get("/api/v1/market/nifty")
        options = await client.get("/api/v1/market/nifty/options")
        candles = await client.get("/api/v1/market/nifty/candles?interval=1m")
        account = await client.get("/api/v1/account/summary")
        orders = await client.get("/api/v1/account/orders")

    assert nifty.status_code == options.status_code == candles.status_code == 200
    assert account.status_code == orders.status_code == 200
    assert nifty.json()["last_price"] == "25001.50"
    assert nifty.json()["source_observed_at"] == "2026-08-03T09:15:00Z"
    assert options.json()["contracts"][0]["strike"] == "25000"
    assert candles.json()["candles"][0]["source_opened_at"] == "2026-08-03T09:15:00Z"
    assert account.json()["available_cash"] == "100000"
    assert account.json()["source_observed_at"] == "2026-08-03T09:15:00Z"
    assert orders.json()["orders"][0]["order_id"] == "ord-1"
    assert "provider_symbol" not in " ".join(
        [nifty.text, options.text, candles.text, account.text, orders.text]
    )


@pytest.mark.asyncio
async def test_required_missing_live_state_returns_a_structured_503() -> None:
    class MissingState(DashboardState):
        async def get_quote(self, instrument_key: str) -> QuoteState | None:
            return None

    app = create_app(
        AppContainer(readiness=Ready(), repository=DashboardRepository(), live_state=MissingState())
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty")

    assert response.status_code == 503
    assert response.json()["code"] == "LIVE_STATE_UNAVAILABLE"
    assert response.json()["occurred_at"].endswith("Z")


@pytest.mark.asyncio
async def test_orders_with_naive_source_timestamps_are_not_exposed() -> None:
    class NaiveOrders(DashboardRepository):
        async def latest_broker_snapshots(self, snapshot_kind: str) -> list[BrokerSnapshotRecord]:
            assert snapshot_kind == "orders"
            return [
                BrokerSnapshotRecord(
                    snapshot_kind="orders",
                    snapshot_key="primary",
                    source="groww_live",
                    idempotency_key="ignored",
                    observed_at=AT,
                    raw_payload={
                        "orders": [
                            {
                                "order_id": "ord-1",
                                "instrument_token": "NIFTY26AUG25000CE",
                                "side": "BUY",
                                "quantity": 75,
                                "status": "COMPLETE",
                                "created_at": "2026-08-03T09:14:00",
                                "updated_at": "2026-08-03T09:15:00",
                            }
                        ]
                    },
                )
            ]

    app = create_app(
        AppContainer(readiness=Ready(), repository=NaiveOrders(), live_state=DashboardState())
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/account/orders")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_all_approved_routes_are_get_only_and_cors_is_localhost_limited(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/v1/system/status")).status_code == 200
        assert (await client.head("/health/live")).status_code == 200
        response = await client.options(
            "/api/v1/market/nifty",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
        )
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        denied = await client.options(
            "/api/v1/market/nifty",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in denied.headers
        assert (await client.post("/api/v1/market/nifty")).status_code == 405
