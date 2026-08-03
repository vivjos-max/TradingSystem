"""Runnable worker composition for the private read-only dashboard."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from hashlib import sha256
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
from typing import Any

from trading_system.brokers.ports import BrokerUnavailable
from growwapi import GrowwFeed
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trading_system.brokers.groww.auth import GrowwAuthenticator
from trading_system.brokers.groww.client import GrowwReadClient
from trading_system.domain.models import Instrument, NiftyUniverse
from trading_system.market.candles import CandleBuilder
from trading_system.market.instruments import resolve_nifty_universe
from trading_system.market.session import NseSessionCalendar
from trading_system.persistence.postgres import PostgresRepository
from trading_system.persistence.redis_state import (
    AccountSummaryState,
    OptionChainState,
    QuoteState,
    RedisLiveState,
)
from trading_system.services.snapshot import SnapshotBundle
from trading_system.services.readiness import ReadinessCoordinator
from trading_system.services.snapshot import SnapshotService
from trading_system.settings import Settings, get_settings
from trading_system.worker.feed import GrowwFeedWorker


ACCOUNT_KEY = "primary"
OPTION_CHAIN_KEY = "nifty-current"
NEAREST_FUTURE_QUOTE_KEY = "nifty-nearest-future"


@dataclass(frozen=True, slots=True)
class AuthenticatedSessionFactory:
    """Exchange TOTP credentials for a fresh in-memory SDK session each time."""

    settings: object
    authenticator: Any
    broker_factory: Callable[[object], object]
    feed_factory: Callable[[object], object]

    def create(self) -> tuple[object, object]:
        sdk_client = self.authenticator.create_client(self.settings)
        return self.broker_factory(sdk_client), self.feed_factory(sdk_client)


async def acquire_authenticated_session(
    sessions: AuthenticatedSessionFactory,
    readiness: Any,
    repository: Any,
    *,
    sleeper: Callable[[float], object],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[object, object]:
    """Remain alive and explicitly blocked while Groww authentication is unavailable."""
    while True:
        try:
            return sessions.create()
        except BrokerUnavailable as error:
            observed_at = clock()
            await readiness.dependency_blocked("groww-auth")
            await repository.save_health_transition(
                service="groww-auth",
                state="blocked",
                source="worker",
                idempotency_key=_key(
                    "groww-auth", f"{error.code}:{observed_at.isoformat()}"
                ),
                observed_at=observed_at,
                raw_payload={
                    "code": error.code,
                    "correlation_id": error.correlation_id,
                },
            )
            result = sleeper(5.0)
            if inspect.isawaitable(result):
                await result


def _source(value: object) -> str:
    return str(value) if value is not None else "groww_live"


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _instrument_payload(instrument: Instrument) -> dict[str, object]:
    return {
        "instrument_token": instrument.instrument_token,
        "symbol": instrument.symbol,
        "exchange": instrument.exchange,
        "instrument_type": instrument.instrument_type,
        "expiry": None if instrument.expiry is None else instrument.expiry.isoformat(),
        "strike": _decimal(instrument.strike),
        "option_type": instrument.option_type,
        "lot_size": instrument.lot_size,
        "tick_size": str(instrument.tick_size),
        "is_reserved": instrument.is_reserved,
        "is_buyable": instrument.is_buyable,
        "source": _source(instrument.source),
    }


def _key(kind: str, observed_at: object) -> str:
    material = f"{kind}\0{observed_at}"
    return f"runtime:{sha256(material.encode()).hexdigest()}"


async def persist_snapshot(
    repository: Any, live_state: Any, snapshot: SnapshotBundle
) -> NiftyUniverse:
    """Persist one complete normalized reconciliation bundle."""
    universe = resolve_nifty_universe(
        snapshot.instruments,
        snapshot.market.last_price,
        snapshot.refreshed_at,
    )
    source = _source(snapshot.market.source)
    observed_at = snapshot.refreshed_at
    instruments = [_instrument_payload(item) for item in snapshot.instruments]
    await repository.save_instrument_import(
        source=source,
        idempotency_key=_key("instruments", observed_at.isoformat()),
        imported_at=observed_at,
        instrument_count=len(instruments),
        raw_payload={"normalized": {"instruments": instruments}},
    )

    quote_payload = {
        "last_price": str(snapshot.market.last_price),
        "source": source,
    }
    await repository.save_observation(
        instrument_key=universe.index.instrument_token,
        source=source,
        idempotency_key=_key("spot", snapshot.market.observed_at.isoformat()),
        observed_at=snapshot.market.observed_at,
        raw_payload=quote_payload,
    )
    await live_state.set_quote(
        universe.index.instrument_token,
        QuoteState(
            instrument_key=universe.index.instrument_token,
            observed_at=snapshot.market.observed_at,
            payload=quote_payload,
        ),
    )

    greek_by_token = {item.instrument.instrument_token: item for item in snapshot.greeks}
    selected = set(universe.selected_strikes)
    contracts: list[dict[str, object]] = []
    for contract in snapshot.option.contracts:
        instrument = contract.instrument
        if instrument.strike not in selected:
            continue
        greek = greek_by_token.get(instrument.instrument_token)
        contracts.append(
            {
                "instrument_token": instrument.instrument_token,
                "strike": _decimal(instrument.strike),
                "option_type": instrument.option_type,
                "ltp": None,
                "bid": None,
                "ask": None,
                "spread": None,
                "volume": None,
                "open_interest": None,
                "implied_volatility": _decimal(
                    None if greek is None else greek.implied_volatility
                ),
                "delta": _decimal(None if greek is None else greek.delta),
                "gamma": _decimal(None if greek is None else greek.gamma),
                "theta": _decimal(None if greek is None else greek.theta),
                "vega": _decimal(None if greek is None else greek.vega),
                "rho": _decimal(None if greek is None else greek.rho),
            }
        )
    contracts.sort(key=lambda item: (Decimal(str(item["strike"])), str(item["option_type"])))
    chain_payload = {
        "expiry": universe.option_expiry.isoformat(),
        "source": _source(snapshot.option.source),
        "atm_strike": str(universe.atm_strike),
        "selected_strikes": [str(item) for item in universe.selected_strikes],
        "contracts": contracts,
    }
    await repository.save_broker_snapshot(
        snapshot_kind="option_chain",
        snapshot_key=OPTION_CHAIN_KEY,
        source=source,
        idempotency_key=_key("option-chain", observed_at.isoformat()),
        observed_at=observed_at,
        raw_payload={"normalized": chain_payload},
    )
    await live_state.set_option_chain(
        OPTION_CHAIN_KEY,
        OptionChainState(
            chain_key=OPTION_CHAIN_KEY,
            observed_at=observed_at,
            payload=chain_payload,
        ),
    )

    positions = [
        {
            "instrument_token": item.instrument.instrument_token,
            "quantity": item.quantity,
            "average_price": str(item.average_price),
            "last_price": _decimal(item.last_price),
            "realized_pnl": str(item.realized_pnl),
            "unrealized_pnl": _decimal(item.unrealized_pnl),
            "source": _source(item.source),
            "observed_at": (item.observed_at or observed_at).isoformat(),
        }
        for item in snapshot.positions
    ]
    account_payload = {
        "available_cash": str(snapshot.margin.available_cash),
        "used_margin": str(snapshot.margin.used_margin),
        "collateral": str(snapshot.margin.collateral),
        "source": _source(snapshot.margin.source),
        "positions": positions,
    }
    await repository.save_account_snapshot(
        account_key=ACCOUNT_KEY,
        source=source,
        idempotency_key=_key("account", observed_at.isoformat()),
        observed_at=observed_at,
        raw_payload=account_payload,
    )
    await live_state.set_account_summary(
        ACCOUNT_KEY,
        AccountSummaryState(
            account_key=ACCOUNT_KEY,
            observed_at=observed_at,
            payload=account_payload,
        ),
    )

    orders_payload = {
        "account_key": ACCOUNT_KEY,
        "orders": [
            {
                "account_key": ACCOUNT_KEY,
                "order_id": item.order_id,
                "instrument_token": item.instrument.instrument_token,
                "side": item.side,
                "quantity": item.quantity,
                "status": item.status,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in snapshot.orders
        ],
    }
    fills_payload = {
        "account_key": ACCOUNT_KEY,
        "fills": [
            {
                "account_key": ACCOUNT_KEY,
                "fill_id": item.fill_id,
                "order_id": item.order_id,
                "instrument_token": item.instrument.instrument_token,
                "quantity": item.quantity,
                "price": str(item.price),
                "filled_at": item.filled_at.isoformat(),
            }
            for item in snapshot.fills
        ],
    }
    for kind, payload in (("orders", orders_payload), ("fills", fills_payload)):
        await repository.save_broker_snapshot(
            snapshot_kind=kind,
            snapshot_key=ACCOUNT_KEY,
            source=source,
            idempotency_key=_key(kind, observed_at.isoformat()),
            observed_at=observed_at,
            raw_payload={"normalized": payload},
        )
    return universe


async def run_worker(settings: Settings | None = None) -> None:
    """Run the only process allowed to authenticate and read from Groww."""
    configured = settings or get_settings()
    engine = create_async_engine(configured.database_url, pool_pre_ping=True)
    redis_client = Redis.from_url(configured.redis_url, decode_responses=True)
    repository = PostgresRepository(async_sessionmaker(engine, expire_on_commit=False))
    live_state = RedisLiveState(redis_client)
    readiness = ReadinessCoordinator(live_state)
    sessions = AuthenticatedSessionFactory(
        settings=configured,
        authenticator=GrowwAuthenticator(),
        broker_factory=GrowwReadClient,
        feed_factory=GrowwFeed,
    )
    await readiness.dependencies_ready()
    try:
        broker, initial_feed = await acquire_authenticated_session(
            sessions,
            readiness,
            repository,
            sleeper=asyncio.sleep,
        )
        snapshot = await SnapshotService(broker, max_attempts=1).refresh()
        universe = await persist_snapshot(repository, live_state, snapshot)
        await readiness.dependencies_ready()
        await readiness.reconciled()
        pending_feed: list[object] = [initial_feed]

        async def reconcile() -> SnapshotBundle:
            refreshed_broker, refreshed_feed = await acquire_authenticated_session(
                sessions,
                readiness,
                repository,
                sleeper=asyncio.sleep,
            )
            refreshed = await SnapshotService(
                refreshed_broker, max_attempts=1
            ).refresh()
            await persist_snapshot(repository, live_state, refreshed)
            pending_feed[:] = [refreshed_feed]
            return refreshed

        def feed_factory() -> object:
            if not pending_feed:
                raise RuntimeError("feed session must be reconciled before reconnect")
            return pending_feed.pop()

        option_instruments = tuple(
            contract.instrument for contract in universe.option_chain.contracts
        )
        worker = GrowwFeedWorker(
            feed_factory=feed_factory,
            index=universe.index,
            derivatives=(universe.future, *option_instruments),
            repository=repository,
            redis_state=live_state,
            candle_builder=CandleBuilder(NseSessionCalendar()),
            readiness=readiness,
            stale_after_seconds=configured.feed_stale_seconds,
            reconcile=reconcile,
            quote_key=lambda instrument: (
                NEAREST_FUTURE_QUOTE_KEY
                if instrument.instrument_token == universe.future.instrument_token
                else instrument.instrument_token
            ),
        )
        await worker.run()
    finally:
        await redis_client.aclose()
        await engine.dispose()


def main() -> None:
    """Console entry point used by Compose."""
    asyncio.run(run_worker())


__all__ = [
    "ACCOUNT_KEY",
    "AuthenticatedSessionFactory",
    "acquire_authenticated_session",
    "NEAREST_FUTURE_QUOTE_KEY",
    "OPTION_CHAIN_KEY",
    "persist_snapshot",
    "run_worker",
]


if __name__ == "__main__":
    main()
