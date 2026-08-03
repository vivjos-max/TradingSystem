from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from trading_system.brokers.ports import BrokerUnavailable
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
from trading_system.services.snapshot import SnapshotService
from trading_system.worker.main import WorkerStartup


AT = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)
EXPIRY = date(2026, 8, 6)


def instrument(
    token: str,
    instrument_type: str,
    *,
    expiry: date | None = None,
    strike: str | None = None,
    option_type: str | None = None,
) -> Instrument:
    return Instrument(
        instrument_token=token,
        symbol="NIFTY",
        exchange="NSE",
        instrument_type=instrument_type,
        expiry=expiry,
        strike=None if strike is None else Decimal(strike),
        option_type=option_type,
        lot_size=1 if instrument_type == "INDEX" else 75,
        tick_size=Decimal("0.05"),
        source=MarketDataSource.GROWW_LIVE,
    )


INDEX = instrument("NIFTY50", "INDEX")
FUTURE = instrument("NIFTY-FUT", "FUTIDX", expiry=EXPIRY)
CALL = instrument("NIFTY-25000-CE", "OPTIDX", expiry=EXPIRY, strike="25000", option_type="CE")
CONTRACT = OptionContract(instrument=CALL)
QUOTE = Quote(
    instrument_token="NIFTY50",
    last_price=Decimal("25001.50"),
    observed_at=AT,
    source=MarketDataSource.GROWW_LIVE,
)
CHAIN = OptionChain(
    underlying=INDEX,
    expiry=EXPIRY,
    contracts=(CONTRACT,),
    source=MarketDataSource.GROWW_LIVE,
)
MARGIN = MarginSummary(
    available_cash=Decimal("100000"),
    used_margin=Decimal("25000"),
    observed_at=AT,
    source=MarketDataSource.GROWW_LIVE,
)
POSITION = Position(
    instrument=FUTURE,
    quantity=75,
    average_price=Decimal("24950"),
    observed_at=AT,
    source=MarketDataSource.GROWW_LIVE,
)
ORDER = Order(
    order_id="order-1",
    instrument=FUTURE,
    side="BUY",
    quantity=75,
    status="COMPLETE",
    created_at=AT,
    updated_at=AT,
    segment="FNO",
    source=MarketDataSource.GROWW_LIVE,
)
FILL = Fill(
    fill_id="fill-1",
    order_id="order-1",
    instrument=FUTURE,
    quantity=75,
    price=Decimal("24950"),
    filled_at=AT,
    source=MarketDataSource.GROWW_LIVE,
)
GREEK = OptionGreeks(
    instrument=CALL,
    observed_at=AT,
    source=MarketDataSource.GROWW_LIVE,
    delta=Decimal("0.51"),
    gamma=Decimal("0.0012"),
)


class CompleteBroker:
    def __init__(self, *, position_failures: int = 0, greeks_failures: int = 0) -> None:
        self.position_failures = position_failures
        self.greeks_failures = greeks_failures
        self.calls: list[str] = []

    def instruments(self) -> tuple[Instrument, ...]:
        self.calls.append("instruments")
        return INDEX, FUTURE, CALL

    def nifty_quote(self) -> Quote:
        self.calls.append("nifty_quote")
        return QUOTE

    def option_chain(self, expiry: date) -> OptionChain:
        self.calls.append(f"option_chain:{expiry.isoformat()}")
        return CHAIN

    def greeks(self, contracts: tuple[OptionContract, ...]) -> tuple[OptionGreeks, ...]:
        self.calls.append(f"greeks:{contracts[0].instrument_token}")
        if self.greeks_failures:
            self.greeks_failures -= 1
            raise BrokerUnavailable(
                code="GROWW_UNAVAILABLE",
                correlation_id="safe-greeks-correlation",
                summary="Groww is unavailable",
            )
        return (GREEK,)

    def margin(self) -> MarginSummary:
        self.calls.append("margin")
        return MARGIN

    def positions(self) -> tuple[Position, ...]:
        self.calls.append("positions")
        if self.position_failures:
            self.position_failures -= 1
            raise BrokerUnavailable(
                code="GROWW_UNAVAILABLE",
                correlation_id="safe-correlation",
                summary="Groww is unavailable",
            )
        return (POSITION,)

    def orders(self) -> tuple[Order, ...]:
        self.calls.append("orders")
        return (ORDER,)

    def fills(self, orders: tuple[Order, ...]) -> tuple[Fill, ...]:
        self.calls.append(f"fills:{orders[0].order_id}")
        return (FILL,)


@pytest.mark.asyncio
async def test_refresh_reconciles_account_before_initial_market_snapshots() -> None:
    broker = CompleteBroker()
    service = SnapshotService(broker, sleeper=lambda _: None, jitter=lambda _: 0, clock=lambda: AT)

    snapshot = await service.refresh()

    assert broker.calls == [
        "instruments",
        "margin",
        "positions",
        "orders",
        "fills:order-1",
        "nifty_quote",
        "option_chain:2026-08-06",
        "greeks:NIFTY-25000-CE",
    ]
    assert snapshot.instruments == (INDEX, FUTURE, CALL)
    assert snapshot.market == QUOTE
    assert snapshot.option == CHAIN
    assert snapshot.margin == MARGIN
    assert snapshot.positions == (POSITION,)
    assert snapshot.orders == (ORDER,)
    assert snapshot.fills == (FILL,)
    assert snapshot.greeks == (GREEK,)
    assert snapshot.refreshed_at == AT


@pytest.mark.asyncio
async def test_failed_position_read_never_becomes_an_empty_position_assumption() -> None:
    broker = CompleteBroker(position_failures=1)
    service = SnapshotService(
        broker,
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        max_attempts=1,
    )

    with pytest.raises(BrokerUnavailable, match="Groww is unavailable"):
        await service.refresh()

    assert service.last_snapshot is None


@pytest.mark.asyncio
async def test_failed_greeks_read_aborts_the_complete_snapshot() -> None:
    broker = CompleteBroker(greeks_failures=1)
    service = SnapshotService(
        broker,
        sleeper=lambda _: None,
        jitter=lambda _: 0,
        clock=lambda: AT,
        max_attempts=1,
    )

    with pytest.raises(BrokerUnavailable, match="Groww is unavailable"):
        await service.refresh()

    assert service.last_snapshot is None


@pytest.mark.asyncio
async def test_refresh_uses_capped_backoff_and_resets_after_success() -> None:
    broker = CompleteBroker(position_failures=7)
    sleeps: list[float] = []
    service = SnapshotService(
        broker,
        sleeper=lambda delay: sleeps.append(delay),
        jitter=lambda _: 0,
        clock=lambda: AT,
    )

    assert (await service.refresh()).positions == (POSITION,)
    assert sleeps == [1, 2, 4, 8, 16, 30, 30]

    broker.position_failures = 1
    assert (await service.refresh()).positions == (POSITION,)
    assert sleeps[-1] == 1


@pytest.mark.asyncio
async def test_injected_jitter_is_bounded_to_twenty_percent() -> None:
    broker = CompleteBroker(position_failures=1)
    sleeps: list[float] = []
    service = SnapshotService(
        broker,
        sleeper=lambda delay: sleeps.append(delay),
        jitter=lambda _: 1000,
        clock=lambda: AT,
    )

    await service.refresh()

    assert sleeps == [1.2]


class RecordingReadiness:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.dependencies = False
        self.account_reconciled = False
        self.live_update = False

    @property
    def is_ready(self) -> bool:
        return self.dependencies and self.account_reconciled and self.live_update

    async def dependencies_ready(self) -> None:
        self.events.append("dependencies_ready")
        self.dependencies = True

    async def reconciled(self) -> None:
        self.events.append("reconciled")
        self.account_reconciled = True

    async def first_live_update(self) -> None:
        self.events.append("first_live_update")
        self.live_update = True


class FirstUpdateFeedWorker:
    def __init__(self, events: list[str], readiness: RecordingReadiness) -> None:
        self.events = events
        self.readiness = readiness

    async def run(self) -> None:
        self.events.append("subscriptions")
        await self.readiness.first_live_update()


@pytest.mark.asyncio
async def test_worker_startup_preserves_dependency_reconciliation_and_feed_order() -> None:
    events: list[str] = []
    broker = CompleteBroker()
    broker.calls = events
    settings = object()
    repository = object()
    redis_state = object()

    async def start_postgresql(value: object) -> object:
        assert value is settings
        events.append("postgresql")
        return repository

    async def start_redis(value: object) -> object:
        assert value is settings
        events.append("redis")
        return redis_state

    def authenticate(value: object) -> CompleteBroker:
        assert value is settings
        events.append("groww_auth")
        return broker

    def snapshot_factory(value: CompleteBroker) -> SnapshotService:
        assert value is broker
        return SnapshotService(value, sleeper=lambda _: None, jitter=lambda _: 0, clock=lambda: AT)

    def readiness_factory(value: object) -> RecordingReadiness:
        assert value is redis_state
        return RecordingReadiness(events)

    def feed_worker_factory(
        value_repository: object,
        value_redis: object,
        value_broker: CompleteBroker,
        snapshot: object,
        readiness: RecordingReadiness,
    ) -> FirstUpdateFeedWorker:
        assert (value_repository, value_redis, value_broker) == (
            repository,
            redis_state,
            broker,
        )
        assert snapshot is not None
        return FirstUpdateFeedWorker(events, readiness)

    startup = WorkerStartup(
        settings_loader=lambda: events.append("settings") or settings,
        postgresql_starter=start_postgresql,
        redis_starter=start_redis,
        authenticator=authenticate,
        snapshot_factory=snapshot_factory,
        readiness_factory=readiness_factory,
        feed_worker_factory=feed_worker_factory,
    )

    result = await startup.run()

    assert events == [
        "settings",
        "postgresql",
        "redis",
        "groww_auth",
        "dependencies_ready",
        "instruments",
        "margin",
        "positions",
        "orders",
        "fills:order-1",
        "nifty_quote",
        "option_chain:2026-08-06",
        "greeks:NIFTY-25000-CE",
        "reconciled",
        "subscriptions",
        "first_live_update",
    ]
    assert result.snapshot.positions == (POSITION,)
    assert result.readiness.is_ready is True
