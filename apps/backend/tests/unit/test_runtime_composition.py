from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.domain.models import (
    Fill,
    Instrument,
    MarginSummary,
    MarketDataSource,
    OptionChain,
    OptionContract,
    OptionGreeks,
    Order,
    Position,
    Quote,
)
from trading_system.brokers.ports import BrokerUnavailable
from trading_system.services.snapshot import SnapshotBundle
from trading_system.worker.runtime import (
    AuthenticatedSessionFactory,
    acquire_authenticated_session,
    persist_snapshot,
)


AT = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)
EXPIRY = date(2026, 8, 6)


def _instrument(
    token: str,
    kind: str,
    *,
    strike: Decimal | None = None,
    option_type: str | None = None,
) -> Instrument:
    return Instrument(
        instrument_token=token,
        symbol="NIFTY",
        exchange="NSE",
        instrument_type=kind,
        expiry=None if kind == "INDEX" else EXPIRY,
        strike=strike,
        option_type=option_type,
        lot_size=1 if kind == "INDEX" else 75,
        tick_size=Decimal("0.05"),
        source=MarketDataSource.GROWW_LIVE,
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.instrument_imports: list[dict[str, object]] = []
        self.observations: list[dict[str, object]] = []
        self.broker_snapshots: list[dict[str, object]] = []
        self.account_snapshots: list[dict[str, object]] = []

    async def save_instrument_import(self, **values: object) -> bool:
        self.instrument_imports.append(values)
        return True

    async def save_observation(self, **values: object) -> bool:
        self.observations.append(values)
        return True

    async def save_broker_snapshot(self, **values: object) -> bool:
        self.broker_snapshots.append(values)
        return True

    async def save_account_snapshot(self, **values: object) -> bool:
        self.account_snapshots.append(values)
        return True


class RecordingLiveState:
    def __init__(self) -> None:
        self.quotes: list[tuple[str, object]] = []
        self.option_chains: list[tuple[str, object]] = []
        self.accounts: list[tuple[str, object]] = []

    async def set_quote(self, key: str, value: object) -> None:
        self.quotes.append((key, value))

    async def set_option_chain(self, key: str, value: object) -> None:
        self.option_chains.append((key, value))

    async def set_account_summary(self, key: str, value: object) -> None:
        self.accounts.append((key, value))


@pytest.mark.asyncio
async def test_snapshot_persistence_populates_the_approved_api_payload_contracts() -> None:
    index = _instrument("NIFTY50", "INDEX")
    future = _instrument("NIFTY-AUG-FUT", "FUTIDX")
    strikes = tuple(Decimal(24750 + offset * 50) for offset in range(11))
    options = tuple(
        _instrument(
            f"NIFTY-{strike}-{option_type}",
            "OPTIDX",
            strike=strike,
            option_type=option_type,
        )
        for strike in strikes
        for option_type in ("CE", "PE")
    )
    contracts = tuple(OptionContract(instrument=item) for item in options)
    greeks = tuple(
        OptionGreeks(
            instrument=item,
            observed_at=AT,
            source=MarketDataSource.GROWW_LIVE,
            delta=Decimal("0.5"),
            implied_volatility=Decimal("12.5"),
        )
        for item in options
    )
    order = Order(
        order_id="order-1",
        instrument=future,
        side="BUY",
        quantity=75,
        status="COMPLETE",
        created_at=AT,
        updated_at=AT,
        segment="FNO",
        source=MarketDataSource.GROWW_LIVE,
    )
    fill = Fill(
        fill_id="fill-1",
        order_id=order.order_id,
        instrument=future,
        quantity=75,
        price=Decimal("25000"),
        filled_at=AT,
        source=MarketDataSource.GROWW_LIVE,
    )
    snapshot = SnapshotBundle(
        instruments=(index, future, *options),
        market=Quote(
            instrument_token=index.instrument_token,
            last_price=Decimal("25001"),
            observed_at=AT,
            source=MarketDataSource.GROWW_LIVE,
        ),
        option=OptionChain(
            underlying=index,
            expiry=EXPIRY,
            contracts=contracts,
            source=MarketDataSource.GROWW_LIVE,
        ),
        margin=MarginSummary(
            available_cash=Decimal("100000"),
            used_margin=Decimal("25000"),
            observed_at=AT,
            source=MarketDataSource.GROWW_LIVE,
        ),
        positions=(
            Position(
                instrument=future,
                quantity=75,
                average_price=Decimal("24950"),
                realized_pnl=Decimal("100"),
                observed_at=AT,
                source=MarketDataSource.GROWW_LIVE,
            ),
        ),
        orders=(order,),
        fills=(fill,),
        greeks=greeks,
        refreshed_at=AT,
    )
    repository = RecordingRepository()
    live_state = RecordingLiveState()

    universe = await persist_snapshot(repository, live_state, snapshot)

    assert universe.future.instrument_token == "NIFTY-AUG-FUT"
    assert live_state.quotes[0][0] == "NIFTY50"
    chain_payload = live_state.option_chains[0][1].payload
    assert chain_payload["selected_strikes"] == [str(strike) for strike in strikes]
    assert len(chain_payload["contracts"]) == 22
    assert chain_payload["contracts"][0] == {
        "instrument_token": "NIFTY-24750-CE",
        "strike": "24750",
        "option_type": "CE",
        "ltp": None,
        "bid": None,
        "ask": None,
        "spread": None,
        "volume": None,
        "open_interest": None,
        "implied_volatility": "12.5",
        "delta": "0.5",
        "gamma": None,
        "theta": None,
        "vega": None,
        "rho": None,
    }
    account_payload = live_state.accounts[0][1].payload
    assert account_payload["positions"][0]["instrument_token"] == "NIFTY-AUG-FUT"
    durable = {
        item["snapshot_kind"]: item["raw_payload"]["normalized"]
        for item in repository.broker_snapshots
    }
    assert durable["orders"]["account_key"] == "primary"
    assert durable["fills"]["fills"][0]["order_id"] == "order-1"


def test_each_feed_session_reauthenticates_without_retaining_an_access_token() -> None:
    clients = [object(), object()]

    class Authenticator:
        def __init__(self) -> None:
            self.calls = 0

        def create_client(self, settings: object) -> object:
            assert settings is configured
            client = clients[self.calls]
            self.calls += 1
            return client

    configured = object()
    authenticator = Authenticator()
    sessions = AuthenticatedSessionFactory(
        settings=configured,
        authenticator=authenticator,
        broker_factory=lambda sdk: ("broker", sdk),
        feed_factory=lambda sdk: ("feed", sdk),
    )

    first = sessions.create()
    second = sessions.create()

    assert first == (("broker", clients[0]), ("feed", clients[0]))
    assert second == (("broker", clients[1]), ("feed", clients[1]))
    assert authenticator.calls == 2
    assert not hasattr(sessions, "access_token")


@pytest.mark.asyncio
async def test_invalid_groww_access_stays_blocked_and_retries_in_process() -> None:
    expected = (object(), object())

    class Sessions:
        def __init__(self) -> None:
            self.calls = 0

        def create(self) -> tuple[object, object]:
            self.calls += 1
            if self.calls < 3:
                raise BrokerUnavailable(
                    code="GROWW_AUTH_FAILED",
                    correlation_id=f"safe-{self.calls}",
                    summary="Groww authentication failed",
                )
            return expected

    class Readiness:
        def __init__(self) -> None:
            self.blocked: list[str] = []

        async def dependency_blocked(self, dependency: str) -> None:
            self.blocked.append(dependency)

    class HealthRepository:
        def __init__(self) -> None:
            self.transitions: list[dict[str, object]] = []

        async def save_health_transition(self, **values: object) -> bool:
            self.transitions.append(values)
            return True

    sessions = Sessions()
    readiness = Readiness()
    repository = HealthRepository()
    sleeps: list[float] = []

    acquired = await acquire_authenticated_session(
        sessions,
        readiness,
        repository,
        sleeper=lambda delay: sleeps.append(delay),
        clock=lambda: AT,
    )

    assert acquired is expected
    assert sessions.calls == 3
    assert readiness.blocked == ["groww-auth", "groww-auth"]
    assert sleeps == [5.0, 5.0]
    assert [item["raw_payload"]["code"] for item in repository.transitions] == [
        "GROWW_AUTH_FAILED",
        "GROWW_AUTH_FAILED",
    ]
