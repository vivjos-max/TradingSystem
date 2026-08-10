from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import JsonValue

from trading_system.api.dependencies import AppContainer as _AppContainer
from trading_system.api.main import create_app
from trading_system.api.routes.system import _system_state
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


def AppContainer(
    *,
    readiness: Any,
    repository: Any,
    live_state: Any,
    nifty_future_instrument_key: str = "TEST-NEAREST-FUTURE",
    clock=lambda: AT,
    **kwargs: Any,
) -> _AppContainer:
    return _AppContainer(
        readiness=readiness,
        repository=repository,
        live_state=live_state,
        nifty_future_instrument_key=nifty_future_instrument_key,
        clock=clock,
        **kwargs,
    )


class DashboardState:
    async def get_quote(self, instrument_key: str) -> QuoteState | None:
        assert instrument_key in {"NIFTY50", "TEST-NEAREST-FUTURE"}
        return QuoteState(
            instrument_key=instrument_key,
            observed_at=AT,
            payload={
                "last_price": (
                    "25001.50" if instrument_key == "NIFTY50" else "25125.25"
                ),
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
                "atm_strike": "25000",
                "selected_strikes": [str(strike) for strike in range(24750, 25251, 50)],
                "contracts": [
                    {
                        "instrument_token": "NIFTY26AUG25000CE",
                        "strike": "25000",
                        "option_type": "CE",
                        "ltp": "120.50",
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
                "positions": [],
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

    async def recent_candles(
        self, *, instrument_key: str, interval: str, limit: int
    ) -> list[CandleRecord]:
        assert (instrument_key, limit) == ("NIFTY50", 100)
        return [
            candle
            for candle in await self.latest_candles(finalized=True)
            if candle.instrument_key == instrument_key and candle.interval == interval
        ]

    async def latest_broker_snapshots(self, snapshot_kind: str) -> list[BrokerSnapshotRecord]:
        assert snapshot_kind in {"orders", "fills"}
        if snapshot_kind == "fills":
            return [
                BrokerSnapshotRecord(
                    snapshot_kind="fills",
                    snapshot_key="primary",
                    source="groww_live",
                    idempotency_key="ignored-fill",
                    observed_at=AT,
                    raw_payload={"account_key": "primary", "fills": []},
                )
            ]
        return [
            BrokerSnapshotRecord(
                snapshot_kind="orders",
                snapshot_key="primary",
                source="groww_live",
                idempotency_key="ignored",
                observed_at=AT,
                raw_payload={
                    "account_key": "primary",
                    "orders": [
                        {
                            "account_key": "primary",
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
async def test_system_status_exposes_persisted_diagnostics_while_not_ready() -> None:
    class DiagnosticRepository(DashboardRepository):
        async def latest_health_transitions(self) -> list[HealthTransitionRecord]:
            return [
                HealthTransitionRecord(
                    service="groww-feed",
                    state="stale",
                    source="worker",
                    idempotency_key="health:stale",
                    observed_at=AT,
                    raw_payload={"access_token": "must-not-leak"},
                ),
                HealthTransitionRecord(
                    service="account-sync",
                    state="blocked",
                    source="worker",
                    idempotency_key="health:blocked",
                    observed_at=AT + timedelta(seconds=1),
                    raw_payload={"reason": "broker token=must-not-leak"},
                ),
            ]

    app = create_app(
        AppContainer(
            readiness=NotReady(),
            repository=DiagnosticRepository(),
            live_state=DashboardState(),
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/system/status")

    assert response.status_code == 200
    assert response.json()["state"] == "blocked"
    assert response.json()["source_observed_at"] == "2026-08-03T09:15:01Z"
    assert {service["state"] for service in response.json()["services"]} == {
        "stale",
        "blocked",
    }
    assert "token" not in response.text.lower()


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
    assert nifty.json()["spot"]["last_price"] == "25001.50"
    assert nifty.json()["spot"]["source_observed_at"] == "2026-08-03T09:15:00Z"
    assert nifty.json()["nearest_future"]["last_price"] == "25125.25"
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
async def test_nifty_returns_independent_spot_and_composed_nearest_future_snapshots() -> None:
    future_at = AT + timedelta(seconds=7)

    class DualQuoteState(DashboardState):
        async def get_quote(self, instrument_key: str) -> QuoteState | None:
            if instrument_key == "NIFTY50":
                return await super().get_quote(instrument_key)
            assert instrument_key == "TEST-NEAREST-FUTURE"
            return QuoteState(
                instrument_key=instrument_key,
                observed_at=future_at,
                payload={"last_price": "25125.25", "source": "groww_live"},
            )

    container = SimpleNamespace(
        readiness=Ready(),
        repository=DashboardRepository(),
        live_state=DualQuoteState(),
        nifty_instrument_key="NIFTY50",
        nifty_future_instrument_key="TEST-NEAREST-FUTURE",
        option_chain_key="nifty-current",
        account_key="primary",
    )
    app = create_app(container)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty")

    assert response.status_code == 200
    assert response.json()["spot"] == {
        "instrument_key": "NIFTY50",
        "last_price": "25001.50",
        "source": "groww_live",
        "source_observed_at": "2026-08-03T09:15:00Z",
    }
    assert response.json()["nearest_future"] == {
        "instrument_key": "TEST-NEAREST-FUTURE",
        "last_price": "25125.25",
        "source": "groww_live",
        "source_observed_at": "2026-08-03T09:15:07Z",
    }


@pytest.mark.asyncio
async def test_nifty_returns_503_when_the_nearest_future_snapshot_is_missing() -> None:
    class SpotOnlyState(DashboardState):
        async def get_quote(self, instrument_key: str) -> QuoteState | None:
            if instrument_key == "NIFTY50":
                return await super().get_quote(instrument_key)
            return None

    container = SimpleNamespace(
        readiness=Ready(),
        repository=DashboardRepository(),
        live_state=SpotOnlyState(),
        nifty_instrument_key="NIFTY50",
        nifty_future_instrument_key="TEST-NEAREST-FUTURE",
        option_chain_key="nifty-current",
        account_key="primary",
    )
    app = create_app(container)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty")

    assert response.status_code == 503


def _option_app(payload: dict[str, Any]):
    class OptionState(DashboardState):
        async def get_option_chain(self, chain_key: str) -> OptionChainState | None:
            assert chain_key == "nifty-current"
            return OptionChainState(chain_key=chain_key, observed_at=AT, payload=payload)

    return create_app(
        AppContainer(
            readiness=Ready(),
            repository=DashboardRepository(),
            live_state=OptionState(),
            nifty_future_instrument_key="TEST-NEAREST-FUTURE",
        )
    )


_ELEVEN_STRIKES = [str(strike) for strike in range(24750, 25251, 50)]


@pytest.mark.asyncio
async def test_options_exposes_only_allowlisted_populated_market_fields() -> None:
    app = _option_app(
        {
            "expiry": "2026-08-06",
            "source": "groww_live",
            "atm_strike": "25000",
            "selected_strikes": _ELEVEN_STRIKES,
            "contracts": [
                {
                    "instrument_token": "TEST-25000-CE",
                    "strike": "25000",
                    "option_type": "CE",
                    "ltp": "120.50",
                    "bid": "120.25",
                    "ask": "120.75",
                    "spread": "0.50",
                    "volume": 1200,
                    "open_interest": 45000,
                    "implied_volatility": "12.75",
                    "delta": "0.51",
                    "gamma": "0.002",
                    "theta": "-8.25",
                    "vega": "14.50",
                    "rho": "3.10",
                    "provider_secret": "must-not-leak",
                }
            ],
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty/options")

    assert response.status_code == 200
    assert response.json()["atm_strike"] == "25000"
    assert response.json()["selected_strikes"] == _ELEVEN_STRIKES
    assert response.json()["contracts"] == [
        {
            "instrument_token": "TEST-25000-CE",
            "strike": "25000",
            "option_type": "CE",
            "ltp": "120.50",
            "bid": "120.25",
            "ask": "120.75",
            "spread": "0.50",
            "volume": 1200,
            "open_interest": 45000,
            "implied_volatility": "12.75",
            "delta": "0.51",
            "gamma": "0.002",
            "theta": "-8.25",
            "vega": "14.50",
            "rho": "3.10",
        }
    ]
    assert "secret" not in response.text.lower()


@pytest.mark.asyncio
async def test_options_preserves_genuinely_unavailable_values_as_null() -> None:
    app = _option_app(
        {
            "expiry": "2026-08-06",
            "source": "groww_live",
            "atm_strike": "25000",
            "selected_strikes": _ELEVEN_STRIKES,
            "contracts": [
                {
                    "instrument_token": "TEST-25000-PE",
                    "strike": "25000",
                    "option_type": "PE",
                }
            ],
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty/options")

    contract = response.json()["contracts"][0]
    assert response.status_code == 200
    assert {
        key: contract[key]
        for key in (
            "ltp",
            "bid",
            "ask",
            "spread",
            "volume",
            "open_interest",
            "implied_volatility",
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
        )
    } == {
        "ltp": None,
        "bid": None,
        "ask": None,
        "spread": None,
        "volume": None,
        "open_interest": None,
        "implied_volatility": None,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "rho": None,
    }


@pytest.mark.asyncio
async def test_options_bounds_oversized_input_to_atm_plus_five_strikes_each_side() -> None:
    contracts = [
        {
            "instrument_token": f"TEST-{strike}-CE",
            "strike": str(strike),
            "option_type": "CE",
        }
        for strike in range(24700, 25301, 50)
    ]
    app = _option_app(
        {
            "expiry": "2026-08-06",
            "source": "groww_live",
            "atm_strike": "25000",
            "selected_strikes": _ELEVEN_STRIKES,
            "contracts": contracts,
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty/options")

    assert response.status_code == 200
    assert [row["strike"] for row in response.json()["contracts"]] == _ELEVEN_STRIKES


@pytest.mark.asyncio
async def test_options_rejects_conflicting_duplicate_in_window_row_instead_of_returning_23_contracts() -> None:
    contracts = [
        {
            "instrument_token": f"TEST-{strike}-{option_type}",
            "strike": strike,
            "option_type": option_type,
        }
        for strike in _ELEVEN_STRIKES
        for option_type in ("CE", "PE")
    ]
    contracts.append(
        {
            "instrument_token": "CONFLICTING-25000-CE",
            "strike": "25000",
            "option_type": "CE",
        }
    )
    app = _option_app(
        {
            "expiry": "2026-08-06",
            "source": "groww_live",
            "atm_strike": "25000",
            "selected_strikes": _ELEVEN_STRIKES,
            "contracts": contracts,
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty/options")

    assert response.status_code == 503
    assert response.json()["code"] == "LIVE_STATE_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        {},
        {"atm_strike": "25000", "selected_strikes": ["24950", "25000", "25050"]},
        {
            "atm_strike": "25000",
            "selected_strikes": ["24750", "24800", "24850", "24900", "24950", "25000"]
            * 2,
        },
    ],
)
async def test_options_returns_503_for_absent_or_malformed_window_context(
    context: dict[str, object],
) -> None:
    app = _option_app(
        {
            "expiry": "2026-08-06",
            "source": "groww_live",
            "contracts": [],
            **context,
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty/options")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_candles_returns_a_bounded_recent_series_ordered_by_open_time() -> None:
    class RecentCandlesRepository(DashboardRepository):
        async def latest_candles(
            self, *, finalized: bool | None = None
        ) -> list[CandleRecord]:
            raise AssertionError("recent candle route must not use latest-per-identity read")

        async def recent_candles(
            self, *, instrument_key: str, interval: str, limit: int
        ) -> list[CandleRecord]:
            assert (instrument_key, interval, limit) == ("NIFTY50", "1m", 100)
            return [
                CandleRecord(
                    instrument_key="NIFTY50",
                    interval="1m",
                    source="groww_live",
                    idempotency_key="candle:1",
                    opened_at=AT - timedelta(minutes=1),
                    closed_at=AT,
                    open=Decimal("24995"),
                    high=Decimal("25001"),
                    low=Decimal("24994"),
                    close=Decimal("25000"),
                    volume=90,
                    finalized=True,
                    is_late=False,
                    raw_payload={"secret": "must-not-leak"},
                ),
                CandleRecord(
                    instrument_key="NIFTY50",
                    interval="1m",
                    source="reconciled_live",
                    idempotency_key="candle:2",
                    opened_at=AT,
                    closed_at=AT + timedelta(minutes=1),
                    open=Decimal("25000"),
                    high=Decimal("25005"),
                    low=Decimal("24999"),
                    close=Decimal("25001.50"),
                    volume=100,
                    finalized=True,
                    is_late=False,
                    raw_payload={"secret": "must-not-leak"},
                ),
            ]

    app = create_app(
        AppContainer(
            readiness=Ready(),
            repository=RecentCandlesRepository(),
            live_state=DashboardState(),
            nifty_future_instrument_key="TEST-NEAREST-FUTURE",
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/market/nifty/candles?interval=1m")

    assert response.status_code == 200
    assert [row["source_opened_at"] for row in response.json()["candles"]] == [
        "2026-08-03T09:14:00Z",
        "2026-08-03T09:15:00Z",
    ]
    assert [row["source"] for row in response.json()["candles"]] == [
        "groww_live",
        "reconciled_live",
    ]
    assert "secret" not in response.text.lower()


def _account_summary_app(positions: list[dict[str, Any]]):
    class PositionState(DashboardState):
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
                    "positions": cast(JsonValue, positions),
                },
            )

    return create_app(
        AppContainer(
            readiness=Ready(),
            repository=DashboardRepository(),
            live_state=PositionState(),
            nifty_future_instrument_key="TEST-NEAREST-FUTURE",
        )
    )


@pytest.mark.asyncio
async def test_account_summary_includes_allowlisted_current_positions_and_timestamps() -> None:
    app = _account_summary_app(
        [
            {
                "instrument_token": "TEST-POSITION",
                "quantity": 75,
                "average_price": "115.25",
                "last_price": "120.50",
                "realized_pnl": "10.00",
                "unrealized_pnl": "393.75",
                "source": "groww_live",
                "observed_at": "2026-08-03T09:14:59Z",
                "provider_account_id": "must-not-leak",
            }
        ]
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/account/summary")

    assert response.status_code == 200
    assert response.json()["positions"] == [
        {
            "instrument_token": "TEST-POSITION",
            "quantity": 75,
            "average_price": "115.25",
            "last_price": "120.50",
            "realized_pnl": "10.00",
            "unrealized_pnl": "393.75",
            "source": "groww_live",
            "source_observed_at": "2026-08-03T09:14:59Z",
        }
    ]
    assert "account_id" not in response.text.lower()


@pytest.mark.asyncio
async def test_account_summary_supports_an_explicitly_empty_position_list() -> None:
    app = _account_summary_app([])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/account/summary")

    assert response.status_code == 200
    assert response.json()["positions"] == []


_EXCHANGE_NOW = datetime(2026, 8, 3, 0, 5, tzinfo=ZoneInfo("Asia/Kolkata"))


def _account_snapshot(
    snapshot_kind: str,
    payload: dict[str, Any],
    *,
    snapshot_key: str = "primary",
    observed_at: datetime = AT,
) -> BrokerSnapshotRecord:
    return BrokerSnapshotRecord(
        snapshot_kind=snapshot_kind,
        snapshot_key=snapshot_key,
        source="groww_live",
        idempotency_key=f"{snapshot_kind}:{snapshot_key}",
        observed_at=observed_at,
        raw_payload=payload,
    )


def _reconciliation_app(
    orders: list[BrokerSnapshotRecord], fills: list[BrokerSnapshotRecord]
):
    class ReconciliationRepository(DashboardRepository):
        async def latest_broker_snapshots(
            self, snapshot_kind: str
        ) -> list[BrokerSnapshotRecord]:
            if snapshot_kind == "orders":
                return orders
            if snapshot_kind == "fills":
                return fills
            raise AssertionError(f"unexpected snapshot kind: {snapshot_kind}")

    container = SimpleNamespace(
        readiness=Ready(),
        repository=ReconciliationRepository(),
        live_state=DashboardState(),
        nifty_instrument_key="NIFTY50",
        nifty_future_instrument_key="TEST-NEAREST-FUTURE",
        option_chain_key="nifty-current",
        account_key="primary",
        clock=lambda: _EXCHANGE_NOW,
    )
    return create_app(container)  # type: ignore[arg-type]


def _current_day_orders_payload() -> dict[str, Any]:
    return {
        "account_key": "primary",
        "orders": [
            {
                "account_key": "primary",
                "order_id": "current-filled",
                "instrument_token": "TEST-CE",
                "side": "BUY",
                "quantity": 75,
                "status": "COMPLETE",
                "created_at": "2026-08-02T18:31:00Z",
                "updated_at": "2026-08-02T18:32:00Z",
            },
            {
                "account_key": "primary",
                "order_id": "current-unfilled",
                "instrument_token": "TEST-PE",
                "side": "SELL",
                "quantity": 75,
                "status": "OPEN",
                "created_at": "2026-08-02T18:32:00Z",
                "updated_at": "2026-08-02T18:33:00Z",
            },
            {
                "account_key": "primary",
                "order_id": "prior-exchange-day",
                "instrument_token": "TEST-OLD",
                "side": "BUY",
                "quantity": 75,
                "status": "COMPLETE",
                "created_at": "2026-08-02T18:29:00Z",
                "updated_at": "2026-08-02T18:29:30Z",
            },
        ],
    }


def _current_day_fills_payload() -> dict[str, Any]:
    return {
        "account_key": "primary",
        "fills": [
            {
                "account_key": "primary",
                "fill_id": "fill-matched",
                "order_id": "current-filled",
                "instrument_token": "TEST-CE",
                "quantity": 75,
                "price": "120.50",
                "filled_at": "2026-08-02T18:31:30Z",
            },
            {
                "account_key": "primary",
                "fill_id": "fill-unmatched",
                "order_id": "missing-order",
                "instrument_token": "TEST-UNKNOWN",
                "quantity": 25,
                "price": "10.00",
                "filled_at": "2026-08-02T18:34:00Z",
            },
        ],
    }


@pytest.mark.asyncio
async def test_orders_reconciles_current_exchange_day_fills_with_source_timestamps() -> None:
    app = _reconciliation_app(
        [_account_snapshot("orders", _current_day_orders_payload())],
        [
            _account_snapshot(
                "fills", _current_day_fills_payload(), observed_at=AT + timedelta(seconds=2)
            )
        ],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/account/orders")

    assert response.status_code == 200
    assert response.json()["orders_source_observed_at"] == "2026-08-03T09:15:00Z"
    assert response.json()["fills_source_observed_at"] == "2026-08-03T09:15:02Z"
    assert [row["order_id"] for row in response.json()["orders"]] == [
        "current-filled",
        "current-unfilled",
    ]
    assert response.json()["orders"][0]["reconciliation"] == "matched"
    assert [fill["fill_id"] for fill in response.json()["orders"][0]["fills"]] == [
        "fill-matched"
    ]
    assert response.json()["orders"][1]["reconciliation"] == "unmatched"
    assert response.json()["orders"][1]["fills"] == []
    assert [fill["fill_id"] for fill in response.json()["unmatched_fills"]] == [
        "fill-unmatched"
    ]
    assert "prior-exchange-day" not in response.text


@pytest.mark.asyncio
async def test_orders_returns_503_when_the_account_fill_snapshot_is_missing() -> None:
    app = _reconciliation_app(
        [_account_snapshot("orders", _current_day_orders_payload())], []
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/account/orders")

    assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_kind", ["orders", "fills"])
async def test_orders_returns_503_for_cross_account_records(snapshot_kind: str) -> None:
    orders_payload = _current_day_orders_payload()
    fills_payload = _current_day_fills_payload()
    row_key = "orders" if snapshot_kind == "orders" else "fills"
    rows = orders_payload[row_key] if snapshot_kind == "orders" else fills_payload[row_key]
    assert isinstance(rows, list)
    rows[0]["account_key"] = "secondary"
    app = _reconciliation_app(
        [_account_snapshot("orders", orders_payload)],
        [_account_snapshot("fills", fills_payload)],
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/account/orders")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_orders_with_naive_source_timestamps_are_not_exposed() -> None:
    class NaiveOrders(DashboardRepository):
        async def latest_broker_snapshots(self, snapshot_kind: str) -> list[BrokerSnapshotRecord]:
            if snapshot_kind == "fills":
                return await super().latest_broker_snapshots(snapshot_kind)
            assert snapshot_kind == "orders"
            return [
                BrokerSnapshotRecord(
                    snapshot_kind="orders",
                    snapshot_key="primary",
                    source="groww_live",
                    idempotency_key="ignored",
                    observed_at=AT,
                    raw_payload={
                        "account_key": "primary",
                        "orders": [
                            {
                                "account_key": "primary",
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
async def test_orders_select_only_the_configured_account_snapshot() -> None:
    class TwoAccounts(DashboardRepository):
        async def latest_broker_snapshots(self, snapshot_kind: str) -> list[BrokerSnapshotRecord]:
            if snapshot_kind == "fills":
                return await super().latest_broker_snapshots(snapshot_kind)
            assert snapshot_kind == "orders"
            return [
                BrokerSnapshotRecord(
                    snapshot_kind="orders",
                    snapshot_key="primary",
                    source="groww_live",
                    idempotency_key="ignored",
                    observed_at=AT,
                    raw_payload={
                        "account_key": "primary",
                        "orders": [
                            {
                                "account_key": "primary",
                                "order_id": "primary-order",
                                "instrument_token": "NIFTY26AUG25000CE",
                                "side": "BUY",
                                "quantity": 75,
                                "status": "COMPLETE",
                                "created_at": "2026-08-03T09:14:00Z",
                                "updated_at": "2026-08-03T09:15:00Z",
                            }
                        ]
                    },
                ),
                BrokerSnapshotRecord(
                    snapshot_kind="orders",
                    snapshot_key="secondary",
                    source="groww_live",
                    idempotency_key="ignored",
                    observed_at=AT + timedelta(minutes=1),
                    raw_payload={
                        "account_key": "secondary",
                        "orders": [
                            {
                                "account_key": "secondary",
                                "order_id": "secondary-order",
                                "instrument_token": "NIFTY26AUG25000PE",
                                "side": "SELL",
                                "quantity": 75,
                                "status": "COMPLETE",
                                "created_at": "2026-08-03T09:15:00Z",
                                "updated_at": "2026-08-03T09:16:00Z",
                            }
                        ]
                    },
                ),
            ]

    app = create_app(
        AppContainer(readiness=Ready(), repository=TwoAccounts(), live_state=DashboardState())
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/account/orders")

    assert response.status_code == 200
    assert response.json()["orders"][0]["order_id"] == "primary-order"


@pytest.mark.asyncio
async def test_orders_return_503_when_the_configured_account_has_no_snapshot() -> None:
    class SecondaryOnly(DashboardRepository):
        async def latest_broker_snapshots(self, snapshot_kind: str) -> list[BrokerSnapshotRecord]:
            if snapshot_kind == "fills":
                return await super().latest_broker_snapshots(snapshot_kind)
            assert snapshot_kind == "orders"
            return [
                BrokerSnapshotRecord(
                    snapshot_kind="orders",
                    snapshot_key="secondary",
                    source="groww_live",
                    idempotency_key="ignored",
                    observed_at=AT,
                    raw_payload={"orders": []},
                )
            ]

    app = create_app(
        AppContainer(readiness=Ready(), repository=SecondaryOnly(), live_state=DashboardState())
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/account/orders")

    assert response.status_code == 503


@pytest.mark.parametrize(
    ("service_state", "expected_system_state"),
    [
        ("healthy", "live"),
        ("live", "live"),
        ("market_closed", "market_closed"),
        ("degraded", "degraded"),
        ("stale", "stale"),
        ("unavailable", "blocked"),
        ("blocked", "blocked"),
        ("unknown", "blocked"),
    ],
)
def test_system_status_normalizes_every_supported_service_health_state(
    service_state: str, expected_system_state: str
) -> None:
    assert _system_state([service_state]) == expected_system_state


@pytest.mark.asyncio
async def test_readiness_is_refreshed_for_starting_ready_and_blocked_worker_transitions() -> None:
    class SharedReadiness(NotReady):
        def __init__(self) -> None:
            self.shared_ready = False
            self.cached_ready = False

        @property
        def is_ready(self) -> bool:
            return self.cached_ready

        async def restore(self) -> None:
            self.cached_ready = self.shared_ready

    readiness = SharedReadiness()
    app = create_app(
        AppContainer(readiness=readiness, repository=DashboardRepository(), live_state=DashboardState())
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            readiness.shared_ready = True
            ready = await client.get("/health/ready")
            readiness.shared_ready = False
            blocked = await client.get("/health/ready")

    assert ready.status_code == 200
    assert blocked.status_code == 503


@pytest.mark.asyncio
async def test_readiness_refresh_failure_returns_the_safe_503_even_after_ready() -> None:
    class FailingRefresh(Ready):
        def __init__(self) -> None:
            self.calls = 0

        async def restore(self) -> None:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("redis token=must-not-leak")

    readiness = FailingRefresh()
    app = create_app(
        AppContainer(readiness=readiness, repository=DashboardRepository(), live_state=DashboardState())
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/health/ready")

    assert response.status_code == 503
    assert "token" not in response.text.lower()


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
