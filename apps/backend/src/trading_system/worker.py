from __future__ import annotations

import random
import signal
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from .config import Settings
from .groww import GrowwError, GrowwReadClient
from .ingestion import IngestionItem, IngestionOverflow, IngestionQueue
from .market import (
    CandleBuilder,
    IST,
    SessionState,
    aggregate_candles,
    load_session_calendar,
    resolve_nifty_universe,
    session_at,
)
from .models import (
    DataSource,
    EventOutcome,
    HealthState,
    Instrument,
    MarketObservation,
    NiftyUniverse,
    PersistedEvent,
)
from .store import Store


Clock = Callable[[], datetime]


def bounded_backoff(jitter: Callable[[], float] = random.random) -> Iterator[float]:
    for seconds in (1, 2, 4, 8, 16):
        yield min(30, seconds + max(0, min(1, jitter())))
    while True:
        yield 30


def best_prices(payload: Mapping[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    def prices(side: str) -> list[Decimal]:
        result = []
        book = (
            payload.get(side, payload.get("buy_book" if side == "buyBook" else "sell_book")) or ()
        )
        rows = book.values() if isinstance(book, Mapping) else book
        for row in rows:
            if isinstance(row, Mapping):
                value = row.get("value", row)
                if isinstance(value, Mapping) and value.get("price") is not None:
                    result.append(Decimal(str(value["price"])))
        return result

    buys, sells = prices("buyBook"), prices("sellBook")
    return (max(buys) if buys else None, min(sells) if sells else None)


def feed_is_silent(
    session_open: bool,
    last_feed: datetime | None,
    subscribed_at: datetime,
    now: datetime,
    stale_seconds: int,
) -> bool:
    return session_open and now - (last_feed or subscribed_at) > timedelta(seconds=stale_seconds)


def feed_event_problem(
    event_at: datetime,
    received_at: datetime,
    stale_seconds: int,
    max_clock_skew_seconds: int,
) -> str | None:
    if event_at - received_at > timedelta(seconds=max_clock_skew_seconds):
        return "FEED_EVENT_FUTURE"
    if received_at - event_at > timedelta(seconds=stale_seconds):
        return "FEED_EVENT_STALE"
    return None


def feed_datetime(value: Any) -> datetime:
    timestamp = float(value)
    if timestamp <= 0:
        raise ValueError("feed timestamp must be positive")
    return datetime.fromtimestamp(
        timestamp / 1000 if timestamp >= 100_000_000_000 else timestamp, timezone.utc
    )


def feed_problem_demotes(
    problem: str | None, last_feed: datetime | None, received_at: datetime, stale_seconds: int
) -> bool:
    return bool(
        problem
        and (last_feed is None or received_at - last_feed > timedelta(seconds=stale_seconds))
    )


class FeedRestartRequired(RuntimeError):
    pass


def broker_failure_blocks(error: BaseException) -> bool:
    code = getattr(getattr(error, "detail", None), "code", "")
    return code in {
        "GROWW_AUTH_FAILED",
        "GROWW_PERMISSION_DENIED",
        "GROWW_CONFIG_MISSING",
        "GROWW_SDK_VERSION_UNSUPPORTED",
    }


class ReadinessCoordinator:
    """The four facts that must all survive in shared live state."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self.store, self.clock = store, clock
        self.stores = self.reconciled = self.subscriptions = self.first_feed = False
        self._last_states: dict[str, tuple[HealthState, str | None]] = {}

    @property
    def is_ready(self) -> bool:
        return self.stores and self.reconciled and self.subscriptions and self.first_feed

    def _heartbeat(self, subsystem: str, state: HealthState, reason: str | None = None) -> None:
        now = self.clock()
        transition = (state, reason)
        transition_error: Exception | None = None
        if self._last_states.get(subsystem) != transition and hasattr(
            self.store, "record_health_transition"
        ):
            try:
                self.store.record_health_transition(
                    subsystem=subsystem,
                    state=state,
                    observed_at=now,
                    idempotency_key=f"{state}:{reason}:{now.isoformat()}",
                    reason=reason,
                )
            except Exception as exc:
                transition_error = exc
        self.store.set_live_value(
            "heartbeat",
            {
                "subsystem": subsystem,
                "state": state,
                "reason": reason,
                "source": DataSource.LOCAL_RECONSTRUCTED,
                "source_at": now,
            },
            identifier=subsystem,
        )
        self._last_states[subsystem] = transition
        if transition_error is not None:
            raise transition_error

    def dependencies_ready(self) -> None:
        self.stores = True
        self._heartbeat("stores", HealthState.HEALTHY)

    def account_reconciled(self) -> None:
        self.reconciled = True
        self._heartbeat("account_reconciliation", HealthState.HEALTHY)

    def subscriptions_active(self) -> None:
        self.subscriptions = True
        self._heartbeat("subscriptions", HealthState.HEALTHY)

    def first_live_update(self) -> None:
        self.first_feed = True
        self._heartbeat("feed", HealthState.HEALTHY)

    def blocked(self, subsystem: str, reason: str) -> None:
        if subsystem == "stores":
            self.stores = False
        elif subsystem == "account_reconciliation":
            self.reconciled = False
        elif subsystem == "subscriptions":
            self.subscriptions = False
        elif subsystem == "feed":
            self.first_feed = False
        self._heartbeat(subsystem, HealthState.BLOCKED, reason)


class Worker:
    REFRESH_SECONDS = 60

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        authenticate: Callable[[Settings], Any] = GrowwReadClient.authenticate,
        clock: Clock = lambda: datetime.now(timezone.utc),
        jitter: Callable[[], float] = random.random,
        calendar: Any = None,
    ) -> None:
        self.settings, self.store = settings, store
        self.authenticate, self.clock = authenticate, clock
        self.readiness = ReadinessCoordinator(store, clock)
        self.client: Any = None
        self.feed: Any = None
        self.universe: NiftyUniverse | None = None
        self.instruments: tuple[Instrument, ...] = ()
        self.builder = CandleBuilder()
        self._minutes: dict[str, list[Any]] = {}
        self._last_account: datetime | None = None
        self._last_options: datetime | None = None
        self._instrument_day: date | None = None
        self._last_feed: datetime | None = None
        self._subscribed_at: datetime | None = None
        self._retry_at: datetime | None = None
        self._last_pulse: datetime | None = None
        self._store_lost = False
        self._jitter = jitter
        self._backoff = bounded_backoff(jitter)
        self.calendar = calendar or load_session_calendar(
            Path(__file__).parents[2] / "config" / "nse_sessions_2026.json"
        )
        self._session_open = False
        self._stopping = threading.Event()
        self._fatal = threading.Event()
        self._fatal_reason: str | None = None
        self._state_lock = threading.Lock()
        self._feed_generation = uuid4().hex
        self._event_identities: set[str] = set()
        self._latest_event_at: dict[str, datetime] = {}
        self._ingestion = (
            IngestionQueue(
                store,
                capacity=settings.ingestion_queue_capacity,
                batch_size=settings.ingestion_batch_size,
            )
            if isinstance(store, Store)
            else None
        )

    def startup(self) -> None:
        if not self.settings.groww_configured:
            raise GrowwError("GROWW_CONFIG_MISSING", "Groww TOTP credentials are not configured")
        self.store.healthcheck()
        self.store.rebuild_live_state()
        self.readiness.dependencies_ready()
        self._update_session()
        self._reconstruct_candles(self.clock())
        self.client = self.authenticate(self.settings)
        self.instruments = tuple(self.client.instruments())
        now = self.clock()
        provisional = resolve_nifty_universe(
            self.instruments,
            self._middle_strike(self.instruments),
            now.date(),
            self.settings.option_window,
        )
        spot = self.client.spot_quote(provisional.spot, now)
        self.universe = resolve_nifty_universe(
            self.instruments, spot.price, now.date(), self.settings.option_window
        )
        market, options = self.client.market_snapshot(self.universe, now)
        account = self.client.account_snapshot(now)
        orders = self.client.orders_and_fills()
        self._persist_initial(now, market, options, account, orders)
        self._last_account = self._last_options = now
        self._instrument_day = now.date()
        self.readiness.account_reconciled()
        self.readiness._heartbeat("market", HealthState.HEALTHY)

    def _reconstruct_candles(self, now: datetime) -> None:
        if not hasattr(self.store, "accepted_observations"):
            return
        local = now.astimezone(IST)
        since = local.replace(hour=9, minute=15, second=0, microsecond=0)
        builder = CandleBuilder(DataSource.DURABLE_RECONSTRUCTED)
        for observation in self.store.accepted_observations(since=since):
            builder.add(observation)
        minutes = builder.finalize_before(now)
        for candle in minutes:
            self.store.upsert_candle(candle)
        for size in (5, 15):
            for candle in aggregate_candles(minutes, size):
                self.store.upsert_candle(candle)

    @staticmethod
    def _middle_strike(instruments: tuple[Instrument, ...]) -> Decimal:
        strikes = sorted({item.strike for item in instruments if item.strike is not None})
        if not strikes:
            raise ValueError("instrument master has no option strikes")
        # ponytail: bootstraps the first quote from the median listed strike; a direct spot quote
        # adapter method can replace this if the broker exposes one without a resolved universe.
        return strikes[len(strikes) // 2]

    def _append(
        self,
        kind: str,
        key: str,
        at: datetime,
        raw: Mapping[str, Any],
        value: Any,
        *,
        outcome: EventOutcome = EventOutcome.ACCEPTED,
    ) -> bool:
        normalized = value.model_dump(mode="python") if hasattr(value, "model_dump") else value
        return self.store.append_event(
            kind=kind,
            source="GROWW",
            idempotency_key=key,
            event_at=at,
            received_at=self.clock(),
            raw=raw,
            normalized=normalized,
            outcome=outcome,
            late=outcome is not EventOutcome.ACCEPTED,
            event_identity=key,
            feed_generation=self._feed_generation,
        )

    def _persist_feed_event(
        self,
        *,
        kind: str,
        identity: str,
        event_at: datetime,
        received_at: datetime,
        raw: Mapping[str, Any],
        normalized: Any,
        outcome: EventOutcome,
        apply: Callable[[], None] | None = None,
        detail: str | None = None,
    ) -> None:
        value = (
            normalized.model_dump(mode="python")
            if hasattr(normalized, "model_dump")
            else normalized
        )
        key = (
            identity if outcome is EventOutcome.ACCEPTED else f"{identity}:{outcome}:{uuid4().hex}"
        )
        event = PersistedEvent(
            kind=kind,
            source="GROWW",
            idempotency_key=key,
            event_identity=identity,
            event_at=event_at,
            received_at=received_at,
            processed_at=self.clock(),
            raw=dict(raw),
            normalized=dict(value),
            outcome=outcome,
            feed_generation=self._feed_generation,
            rejection_detail=detail,
        )
        if self._ingestion is None:
            inserted = self.store.append_event(
                kind=event.kind,
                source=event.source,
                idempotency_key=event.idempotency_key,
                event_identity=event.event_identity,
                event_at=event.event_at,
                received_at=event.received_at,
                processed_at=event.processed_at,
                raw=event.raw,
                normalized=event.normalized,
                outcome=event.outcome,
                feed_generation=event.feed_generation,
                rejection_detail=event.rejection_detail,
                late=event.outcome is not EventOutcome.ACCEPTED,
            )
            if inserted and apply is not None:
                apply()
            return
        try:
            self._ingestion.submit(IngestionItem(event, apply))
        except (IngestionOverflow, RuntimeError) as exc:
            self._fatal_reason = (
                "INGESTION_QUEUE_OVERFLOW"
                if isinstance(exc, IngestionOverflow)
                else "PERSISTENCE_WORKER_UNAVAILABLE"
            )
            self._fatal.set()

    def _claim(self, identity: str) -> bool:
        if hasattr(self.store, "claim_event"):
            return self.store.claim_event(identity)
        if identity in self._event_identities:
            return False
        self._event_identities.add(identity)
        return True

    def _persist_initial(
        self, now: datetime, market: Any, options: Any, account: Any, orders: Any
    ) -> None:
        audit = self.client.audit_raw
        self._append(
            "instrument_import",
            f"instruments:{now.isoformat()}",
            now,
            audit.get("instruments", {}),
            {
                "instruments": [item.model_dump(mode="python") for item in self.instruments],
                "source_at": now,
            },
        )
        market_raw = audit.get("market_snapshot", {})
        self._append("market_snapshot", f"market:{now.isoformat()}", now, market_raw, market)
        self._append(
            "option_chain",
            f"options:{now.isoformat()}",
            now,
            {"option_chain": market_raw.get("option_chain", {})},
            options,
        )
        account_value = {
            "summary": account.model_dump(mode="python"),
            "orders": [item.model_dump(mode="python") for item in orders],
            "source": DataSource.GROWW_LIVE,
            "source_at": account.source_at,
        }
        self._append(
            "account_snapshot",
            f"account:{now.isoformat()}",
            now,
            {
                "account": audit.get("account_snapshot", {}),
                "orders_and_fills": audit.get("orders_and_fills", {}),
            },
            account_value,
        )
        self.store.set_live_value("account", account_value)
        self._store_market(market, options)

    def _store_market(self, market: Any, options: Any) -> None:
        expected = (
            {item.trading_symbol for item in self.universe.options} if self.universe else set()
        )
        actual = {item.instrument.trading_symbol for item in options.contracts}
        if not expected or actual != expected:
            raise GrowwError("GROWW_CONTRACT_MISMATCH", "required option window is incomplete")
        for market_quote in (market.spot, market.future):
            if market_quote is not None:
                self.store.set_live_value(
                    "quote", market_quote, identifier=market_quote.instrument.trading_symbol
                )
        for contract in options.contracts:
            current = self.store.get_live_value(
                "option-contract", identifier=contract.instrument.trading_symbol
            ) or {"instrument": contract.instrument.model_dump(mode="python")}
            for name in (
                "ltp",
                "volume",
                "open_interest",
                "implied_volatility",
                "delta",
                "gamma",
                "theta",
                "vega",
            ):
                value = getattr(contract, name)
                if value is not None:
                    current[name] = {
                        "value": value,
                        "source": contract.source,
                        "source_at": contract.source_at,
                    }
            current["option_chain_snapshot"] = {
                "value": True,
                "source": contract.source,
                "source_at": contract.source_at,
            }
            self.store.set_live_value(
                "option-contract", current, identifier=contract.instrument.trading_symbol
            )
        self.store.set_live_value(
            "option-chain",
            {
                "expiry": options.expiry,
                "atm_strike": options.atm_strike,
                "contracts": [item.instrument.trading_symbol for item in options.contracts],
                "source": options.source,
                "source_at": options.source_at,
                "feed_generation": self._feed_generation,
            },
        )
        self.readiness._heartbeat("market", HealthState.HEALTHY)

    def start_feed(self, feed: Any) -> None:
        if self.universe is None:
            raise RuntimeError("worker startup has not completed")
        self.feed = feed
        feed.subscribe_index_value(
            (self.universe.spot,),
            lambda meta, source=feed: self._price_callback(
                meta, source.get_index_value, "value", source
            ),
        )
        derivatives = (self.universe.future, *self.universe.options)
        try:
            feed.subscribe_ltp(
                derivatives,
                lambda meta, source=feed: self._price_callback(meta, source.get_ltp, "ltp", source),
            )
            feed.subscribe_market_depth(
                derivatives,
                lambda meta, source=feed: self._depth_callback(meta, source),
            )
        except Exception:
            self._unsubscribe()
            raise
        self._write_subscription(self.universe)
        self._subscribed_at = self.clock()
        self.readiness.subscriptions_active()

    def _write_subscription(self, universe: NiftyUniverse) -> None:
        now = self.clock()
        value = {
            "active": True,
            "spot": universe.spot.trading_symbol,
            "future": universe.future.trading_symbol,
            "options": [item.trading_symbol for item in universe.options],
            "expiry": universe.option_expiry,
            "atm_strike": universe.atm_strike,
            "source": DataSource.LOCAL_RECONSTRUCTED,
            "source_at": now,
            "feed_generation": self._feed_generation,
        }
        self.store.set_live_value("subscription", value)
        self._append("subscription", f"subscription:{now.isoformat()}", now, {}, value)

    @staticmethod
    def _payload_for_meta(payload: Mapping[str, Any], meta: Mapping[str, Any]) -> Mapping[str, Any]:
        exchange, segment = meta.get("exchange"), meta.get("segment")
        key = meta.get("feed_key", meta.get("exchange_token"))
        value: Any = payload
        for part in (exchange, segment, key):
            if part is not None and isinstance(value, Mapping) and part in value:
                value = value[part]
            elif part is not None:
                break
        if isinstance(value, Mapping) and any(
            name in value
            for name in ("value", "ltp", "buyBook", "sellBook", "buy_book", "sell_book")
        ):
            return value
        if key is not None and isinstance(payload.get(str(key)), Mapping):
            return payload[str(key)]
        raise ValueError("feed payload does not contain the changed instrument")

    def _instrument_for_meta(self, meta: Mapping[str, Any]) -> Instrument:
        key = str(meta.get("exchange_token", meta.get("feed_key", "")))
        matches = [item for item in self.instruments if item.exchange_token == key]
        if len(matches) != 1:
            raise ValueError("feed metadata does not identify one instrument")
        return matches[0]

    def _index_callback(self, meta: Mapping[str, Any]) -> None:
        self._price_callback(meta, self.feed.get_index_value, "value")

    def _ltp_callback(self, meta: Mapping[str, Any]) -> None:
        self._price_callback(meta, self.feed.get_ltp, "ltp")

    def _price_callback(
        self,
        meta: Mapping[str, Any],
        read: Callable[[], Mapping[str, Any]],
        field: str,
        source: Any = None,
    ) -> None:
        received_at = self.clock()
        source = source or self.feed
        inactive = source is not self.feed
        if inactive:
            identity = f"inactive:{meta.get('exchange_token', meta.get('feed_key', 'unknown'))}:{received_at.timestamp()}"
            self._persist_feed_event(
                kind="market_observation",
                identity=identity,
                event_at=received_at,
                received_at=received_at,
                raw={"callback": dict(meta)},
                normalized={"feed_generation": self._feed_generation},
                outcome=EventOutcome.INACTIVE_FEED_GENERATION,
            )
            return
        try:
            raw = self._payload_for_meta(read(), meta)
            instrument = self._instrument_for_meta(meta)
            timestamp = raw.get("tsInMillis", raw.get("ts_in_millis"))
            event_at = feed_datetime(timestamp)
            observation = MarketObservation(
                observation_id=f"price:{instrument.exchange_token}:{timestamp}",
                instrument=instrument.trading_symbol,
                price=Decimal(str(raw[field])),
                event_at=event_at,
                received_at=received_at,
                feed_generation=self._feed_generation,
            )
        except (KeyError, TypeError) as exc:
            self._persist_feed_event(
                kind="market_observation",
                identity=f"missing:{received_at.timestamp()}:{meta.get('feed_key', '')}",
                event_at=received_at,
                received_at=received_at,
                raw={"callback": dict(meta)},
                normalized={"feed_generation": self._feed_generation},
                outcome=EventOutcome.MISSING_REQUIRED_DATA,
                detail=type(exc).__name__,
            )
            return
        except Exception as exc:
            self._persist_feed_event(
                kind="market_observation",
                identity=f"malformed:{received_at.timestamp()}:{meta.get('feed_key', '')}",
                event_at=received_at,
                received_at=received_at,
                raw={"callback": dict(meta)},
                normalized={"feed_generation": self._feed_generation},
                outcome=EventOutcome.MALFORMED,
                detail=type(exc).__name__,
            )
            return

        identity = observation.observation_id
        if not self._claim(identity):
            outcome = EventOutcome.DUPLICATE
        elif not self._session_open:
            outcome = EventOutcome.MARKET_CLOSED
        elif event_at - received_at > timedelta(seconds=self.settings.source_clock_skew_seconds):
            outcome = EventOutcome.FUTURE_TIMESTAMP
        elif received_at - event_at > timedelta(seconds=self.settings.feed_stale_seconds):
            outcome = EventOutcome.STALE
        elif event_at < self._latest_event_at.get(instrument.trading_symbol, event_at):
            outcome = EventOutcome.OUT_OF_ORDER
        else:
            outcome = EventOutcome.ACCEPTED

        def apply() -> None:
            with self._state_lock:
                if self.feed is not source or outcome is not EventOutcome.ACCEPTED:
                    return
                if self.builder.add(observation) != "ACCEPTED":
                    return
                self._latest_event_at[instrument.trading_symbol] = event_at
                self._last_feed = received_at
            self.store.set_live_value(
                "quote",
                {
                    "instrument": instrument.model_dump(mode="python"),
                    "price": observation.price,
                    "source": DataSource.LIVE_ACCEPTED,
                    "source_at": event_at,
                    "feed_generation": self._feed_generation,
                },
                identifier=instrument.trading_symbol,
            )
            if instrument.option_type is not None:
                current = self.store.get_live_value(
                    "option-contract", identifier=instrument.trading_symbol
                ) or {"instrument": instrument.model_dump(mode="python")}
                current["ltp"] = {
                    "value": observation.price,
                    "source": DataSource.LIVE_ACCEPTED,
                    "source_at": event_at,
                }
                self.store.set_live_value(
                    "option-contract", current, identifier=instrument.trading_symbol
                )
            self._finalize(received_at, locked=True)
            self.readiness.first_live_update()

        def reject() -> None:
            if outcome in {
                EventOutcome.STALE,
                EventOutcome.FUTURE_TIMESTAMP,
            } and feed_problem_demotes(
                str(outcome), self._last_feed, received_at, self.settings.feed_stale_seconds
            ):
                self.readiness._heartbeat("feed", HealthState.STALE, str(outcome))

        self._persist_feed_event(
            kind="market_observation",
            identity=identity,
            event_at=event_at,
            received_at=received_at,
            raw=raw,
            normalized=observation,
            outcome=outcome,
            apply=apply if outcome is EventOutcome.ACCEPTED else reject,
        )

    def _finalize(self, cutoff: datetime, *, locked: bool = False) -> None:
        if not locked:
            self._state_lock.acquire()
        try:
            self._finalize_locked(cutoff)
        finally:
            if not locked:
                self._state_lock.release()

    def _finalize_locked(self, cutoff: datetime) -> None:
        for candle in self.builder.finalize_before(cutoff):
            self.store.upsert_candle(candle)
            self.store.set_live_value("candle", candle, identifier=candle.instrument, interval="1m")
            minutes = self._minutes.setdefault(candle.instrument, [])
            minutes.append(candle)
            minutes[:] = minutes[-15:]
            for size in (5, 15):
                for aggregate in aggregate_candles(tuple(minutes), size):
                    self.store.upsert_candle(aggregate)
                    self.store.set_live_value(
                        "candle",
                        aggregate,
                        identifier=aggregate.instrument,
                        interval=aggregate.interval,
                    )

    def _depth_callback(self, meta: Mapping[str, Any], source: Any = None) -> None:
        received_at = self.clock()
        source = source or self.feed
        inactive = source is not self.feed
        if inactive:
            self._persist_feed_event(
                kind="feed_depth",
                identity=f"inactive-depth:{received_at.timestamp()}:{meta.get('feed_key', '')}",
                event_at=received_at,
                received_at=received_at,
                raw={"callback": dict(meta)},
                normalized={"feed_generation": self._feed_generation},
                outcome=EventOutcome.INACTIVE_FEED_GENERATION,
            )
            return
        try:
            active_feed = source or self.feed
            raw = self._payload_for_meta(active_feed.get_market_depth(), meta)
            instrument = self._instrument_for_meta(meta)
            timestamp = raw.get("tsInMillis", raw.get("ts_in_millis"))
            event_at = feed_datetime(timestamp)
            bid, ask = best_prices(raw)
        except Exception as exc:
            self._persist_feed_event(
                kind="feed_depth",
                identity=f"malformed-depth:{received_at.timestamp()}:{meta.get('feed_key', '')}",
                event_at=received_at,
                received_at=received_at,
                raw={"callback": dict(meta)},
                normalized={"feed_generation": self._feed_generation},
                outcome=(
                    EventOutcome.MISSING_REQUIRED_DATA
                    if isinstance(exc, (KeyError, TypeError))
                    else EventOutcome.MALFORMED
                ),
                detail=type(exc).__name__,
            )
            return
        identity = f"depth:{instrument.exchange_token}:{timestamp}"
        if not self._claim(identity):
            outcome = EventOutcome.DUPLICATE
        elif not self._session_open:
            outcome = EventOutcome.MARKET_CLOSED
        elif event_at - received_at > timedelta(seconds=self.settings.source_clock_skew_seconds):
            outcome = EventOutcome.FUTURE_TIMESTAMP
        elif received_at - event_at > timedelta(seconds=self.settings.feed_stale_seconds):
            outcome = EventOutcome.STALE
        elif event_at < self._latest_event_at.get(f"depth:{instrument.trading_symbol}", event_at):
            outcome = EventOutcome.OUT_OF_ORDER
        else:
            outcome = EventOutcome.ACCEPTED
        value = {
            "instrument": instrument.trading_symbol,
            "bid": bid,
            "ask": ask,
            "spread": ask - bid if bid is not None and ask is not None else None,
            "source": DataSource.LIVE_ACCEPTED,
            "source_at": event_at,
            "feed_generation": self._feed_generation,
        }

        def apply() -> None:
            if self.feed is not source or outcome is not EventOutcome.ACCEPTED:
                return
            self._latest_event_at[f"depth:{instrument.trading_symbol}"] = event_at
            current = self.store.get_live_value(
                "option-contract", identifier=instrument.trading_symbol
            ) or {"instrument": instrument.model_dump(mode="python")}
            current["bid"] = {
                "value": bid,
                "source": DataSource.LIVE_ACCEPTED,
                "source_at": event_at,
            }
            current["ask"] = {
                "value": ask,
                "source": DataSource.LIVE_ACCEPTED,
                "source_at": event_at,
            }
            current["depth"] = {
                "value": {"bid": bid, "ask": ask, "spread": value["spread"]},
                "source": DataSource.LIVE_ACCEPTED,
                "source_at": event_at,
            }
            self.store.set_live_value(
                "option-contract", current, identifier=instrument.trading_symbol
            )
            self.readiness._heartbeat(
                "depth",
                HealthState.HEALTHY
                if bid is not None and ask is not None
                else HealthState.DEGRADED,
                None if bid is not None and ask is not None else "MISSING_BOOK_SIDE",
            )
            self._last_feed = received_at

        def reject() -> None:
            if outcome in {
                EventOutcome.STALE,
                EventOutcome.FUTURE_TIMESTAMP,
            } and feed_problem_demotes(
                str(outcome), self._last_feed, received_at, self.settings.feed_stale_seconds
            ):
                self.readiness._heartbeat("feed", HealthState.STALE, str(outcome))

        self._persist_feed_event(
            kind="feed_depth",
            identity=identity,
            event_at=event_at,
            received_at=received_at,
            raw=raw,
            normalized=value,
            outcome=outcome,
            apply=apply if outcome is EventOutcome.ACCEPTED else reject,
        )

    def reconcile_account(self) -> None:
        now = self.clock()
        account = self.client.account_snapshot(now)
        orders = self.client.orders_and_fills()
        value = {
            "summary": account.model_dump(mode="python"),
            "orders": [item.model_dump(mode="python") for item in orders],
            "source": DataSource.GROWW_LIVE,
            "source_at": account.source_at,
        }
        audit = self.client.audit_raw
        self._append(
            "account_snapshot",
            f"account:{now.isoformat()}",
            now,
            {
                "account": audit.get("account_snapshot", {}),
                "orders_and_fills": audit.get("orders_and_fills", {}),
            },
            value,
        )
        self.store.set_live_value("account", value)
        self._last_account = now
        self.readiness.account_reconciled()

    def refresh_market(self) -> None:
        now = self.clock()
        market, options = self.client.market_snapshot(self.universe, now)
        new_universe = resolve_nifty_universe(
            self.instruments, market.spot.price, now.date(), self.settings.option_window
        )
        if new_universe != self.universe:
            self.swap_universe(new_universe)
            market, options = self.client.market_snapshot(new_universe, now)
        raw = self.client.audit_raw.get("market_snapshot", {})
        self._append("market_snapshot", f"market:{now.isoformat()}", now, raw, market)
        self._append(
            "option_chain",
            f"options:{now.isoformat()}",
            now,
            {"option_chain": raw.get("option_chain", {})},
            options,
        )
        self._store_market(market, options)
        self._last_options = now
        self.readiness._heartbeat("market", HealthState.HEALTHY)

    def tick(self) -> None:
        now = self.clock()
        if self._fatal.is_set() or (self._ingestion and self._ingestion.failure is not None):
            reason = self._fatal_reason or "PERSISTENCE_WORKER_FAILED"
            self.readiness.blocked("feed", reason)
            raise FeedRestartRequired(reason)
        if self._ingestion is not None:
            metrics = self._ingestion.metrics
            self.store.set_live_value(
                "heartbeat",
                {
                    "subsystem": "ingestion",
                    "state": HealthState.HEALTHY,
                    "source": DataSource.LOCAL_RECONSTRUCTED,
                    "source_at": now,
                    "feed_generation": self._feed_generation,
                    "queue_size": metrics.queued,
                    "queue_peak": metrics.peak,
                    "persisted": metrics.persisted,
                    "overflowed": metrics.overflowed,
                    "postgres_write_ms": metrics.last_write_ms,
                },
                identifier="ingestion",
            )
        self._finalize(now)
        was_open = self._session_open
        self._update_session()
        session_started = self._session_open and not was_open
        if session_started:
            local = now.astimezone(IST)
            self.builder.prune_before(local.replace(hour=9, minute=15, second=0, microsecond=0))
            self._minutes.clear()
        if not self._pulse(now):
            return
        if (
            self._session_open
            and self._last_feed is not None
            and now - self._last_feed > timedelta(seconds=self.settings.feed_stale_seconds)
        ):
            self.readiness._heartbeat("feed", HealthState.STALE, "FEED_STALE")
        if self._retry_at is not None and now < self._retry_at:
            return
        try:
            if (
                session_started
                or self._instrument_day != now.date()
                or (self.universe and self.universe.option_expiry < now.date())
            ):
                self.refresh_instruments()
            if self._last_account is None or now - self._last_account >= timedelta(
                seconds=self.REFRESH_SECONDS
            ):
                self.reconcile_account()
            if self._last_options is None or now - self._last_options >= timedelta(
                seconds=self.REFRESH_SECONDS
            ):
                self.refresh_market()
        except Exception as exc:
            subsystem = (
                "account_reconciliation"
                if self._last_account is None
                or now - self._last_account >= timedelta(seconds=self.REFRESH_SECONDS)
                else "market"
            )
            code = getattr(getattr(exc, "detail", None), "code", "")
            blocked = code in {
                "GROWW_AUTH_FAILED",
                "GROWW_PERMISSION_DENIED",
                "GROWW_CONFIG_MISSING",
            }
            self.readiness._heartbeat(
                subsystem,
                HealthState.BLOCKED if blocked else HealthState.STALE,
                code or "REFRESH_FAILED",
            )
            self._retry_at = now + timedelta(seconds=next(self._backoff))
        else:
            self._retry_at = None
            self._backoff = bounded_backoff(self._jitter)

    def _pulse(self, now: datetime) -> bool:
        if self._last_pulse is not None and now - self._last_pulse < timedelta(seconds=10):
            return self.readiness.stores
        try:
            self.store.healthcheck()
        except Exception:
            self._store_lost = True
            self.readiness.stores = False
            try:
                self.readiness.blocked("stores", "REQUIRED_STORE_UNAVAILABLE")
            except Exception:
                pass
            self._last_pulse = now
            return False
        missing_subscription = self.readiness.subscriptions and not self.store.get_live_value(
            "subscription"
        )
        if self._store_lost or missing_subscription:
            self._recover_store(now)
            self._store_lost = False
        self.readiness.dependencies_ready()
        if self.readiness.reconciled:
            self.readiness._heartbeat("account_reconciliation", HealthState.HEALTHY)
        if self.readiness.subscriptions:
            self.readiness._heartbeat("subscriptions", HealthState.HEALTHY)
            subscription = self.store.get_live_value("subscription")
            if subscription:
                self.store.set_live_value("subscription", subscription)
        if self._last_options is not None:
            state = (
                HealthState.HEALTHY
                if now - self._last_options <= timedelta(seconds=self.REFRESH_SECONDS)
                else HealthState.STALE
            )
            self.readiness._heartbeat(
                "market", state, None if state is HealthState.HEALTHY else "MARKET_SNAPSHOT_STALE"
            )
        self._last_pulse = now
        return True

    def _recover_store(self, now: datetime) -> None:
        self.store.rebuild_live_state()
        self.readiness.reconciled = self.readiness.subscriptions = self.readiness.first_feed = False
        self._last_feed = None
        self._subscribed_at = now
        self.reconcile_account()
        self.refresh_market()
        if self.feed is not None and self.universe is not None:
            self._write_subscription(self.universe)
            self.readiness.subscriptions_active()

    def refresh_instruments(self) -> None:
        now = self.clock()
        instruments = tuple(self.client.instruments())
        current = (
            self.store.get_live_value("quote", identifier=self.universe.spot.trading_symbol)
            if self.universe
            else None
        )
        price = (
            Decimal(str(current["price"]))
            if current and current.get("price")
            else self._middle_strike(instruments)
        )
        new = resolve_nifty_universe(instruments, price, now.date(), self.settings.option_window)
        self.instruments = instruments
        if new != self.universe:
            self.swap_universe(new)
        self._instrument_day = now.date()
        self._append(
            "instrument_import",
            f"instruments:{now.isoformat()}",
            now,
            self.client.audit_raw.get("instruments", {}),
            {
                "instruments": [item.model_dump(mode="python") for item in instruments],
                "source_at": now,
            },
        )

    def _update_session(self) -> None:
        current = session_at(self.calendar, self.clock())
        self._session_open = current.state is SessionState.OPEN
        state = (
            HealthState.BLOCKED if current.state is SessionState.BLOCKED else HealthState.HEALTHY
        )
        self.readiness._heartbeat("market_session", state, current.reason or "MARKET_OPEN")

    def swap_universe(self, new: NiftyUniverse) -> None:
        old = self.universe
        if old is None or self.feed is None:
            self.universe = new
            return
        old_items, new_items = set((old.future, *old.options)), set((new.future, *new.options))
        added, removed = tuple(new_items - old_items), tuple(old_items - new_items)
        new_spot = new.spot != old.spot
        ltp_subscribed = False
        try:
            if new_spot:
                self.feed.subscribe_index_value(
                    (new.spot,),
                    lambda meta, source=self.feed: self._price_callback(
                        meta, source.get_index_value, "value", source
                    ),
                )
            if added:
                self.feed.subscribe_ltp(
                    added,
                    lambda meta, source=self.feed: self._price_callback(
                        meta, source.get_ltp, "ltp", source
                    ),
                )
                ltp_subscribed = True
                self.feed.subscribe_market_depth(
                    added, lambda meta, source=self.feed: self._depth_callback(meta, source)
                )
        except Exception:
            if added:
                self.feed.unsubscribe_market_depth(added)
            if ltp_subscribed:
                self.feed.unsubscribe_ltp(added)
            if new_spot:
                self.feed.unsubscribe_index_value((new.spot,))
            self.readiness._heartbeat(
                "subscriptions", HealthState.DEGRADED, "SUBSCRIPTION_SWAP_FAILED"
            )
            raise
        self.universe = new
        self._write_subscription(new)
        if removed:
            self.feed.unsubscribe_ltp(removed)
            self.feed.unsubscribe_market_depth(removed)
        if new_spot:
            self.feed.unsubscribe_index_value((old.spot,))

    def stop(self) -> None:
        self._stopping.set()
        self._unsubscribe()

    def _unsubscribe(self) -> bool:
        feed, self.feed = self.feed, None
        if feed is None or self.universe is None:
            return True
        derivatives = (self.universe.future, *self.universe.options)
        complete = True
        for unsubscribe, items in (
            (feed.unsubscribe_market_depth, derivatives),
            (feed.unsubscribe_ltp, derivatives),
            (feed.unsubscribe_index_value, (self.universe.spot,)),
        ):
            try:
                unsubscribe(items)
            except Exception:
                complete = False
        return complete

    def run(self) -> None:
        try:
            try:
                self.startup()
            except Exception as exc:
                code = getattr(getattr(exc, "detail", None), "code", "") or "STARTUP_FAILED"
                subsystem = "feed" if code.startswith("GROWW_") else "stores"
                try:
                    self.readiness.blocked(subsystem, code)
                except Exception:
                    pass
                raise

            self.readiness.subscriptions = self.readiness.first_feed = False
            self.readiness._heartbeat("feed", HealthState.STALE, "CONNECTING")
            with self.client.open_feed(self.settings.groww_feed_credential_dir) as feed:
                self.start_feed(feed)
                finished = threading.Event()
                errors: list[BaseException] = []

                def consume() -> None:
                    try:
                        feed.consume()
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        finished.set()

                threading.Thread(
                    target=consume,
                    name=f"groww-feed-{self._feed_generation[:8]}",
                    daemon=True,
                ).start()
                while not self._stopping.wait(1):
                    if finished.is_set():
                        reason = "FEED_CONSUMER_FAILED" if errors else "FEED_CONSUMER_STOPPED"
                        self.readiness.blocked("feed", reason)
                        raise FeedRestartRequired(reason) from (errors[0] if errors else None)
                    self.tick()
                    if self._subscribed_at and feed_is_silent(
                        self._session_open,
                        self._last_feed,
                        self._subscribed_at,
                        self.clock(),
                        self.settings.feed_stale_seconds,
                    ):
                        self.readiness.blocked("feed", "FEED_SILENT_PROCESS_RESTART_REQUIRED")
                        raise FeedRestartRequired("live feed is silent")
        except BaseException as exc:
            if not self._stopping.is_set() and not broker_failure_blocks(exc):
                try:
                    self.readiness.blocked("feed", "FEED_PROCESS_RESTART_REQUIRED")
                except Exception:
                    pass
            raise
        finally:
            self._unsubscribe()
            if self._ingestion is not None:
                self._ingestion.close()


def main() -> None:
    settings = Settings()
    password = (
        quote(settings.postgres_password.get_secret_value(), safe="")
        if settings.postgres_password
        else ""
    )
    user = quote(settings.postgres_user, safe="")
    authority = f"{user}:{password}" if password else user
    database_url = (
        f"postgresql://{authority}@{settings.postgres_host}:"
        f"{settings.postgres_port}/{quote(settings.postgres_db, safe='')}"
    )
    redis_password = (
        quote(settings.redis_password.get_secret_value(), safe="")
        if settings.redis_password
        else ""
    )
    redis_url = (
        f"redis://:{redis_password}@{settings.redis_host}:{settings.redis_port}/0"
        if redis_password
        else f"redis://{settings.redis_host}:{settings.redis_port}/0"
    )
    store = Store(database_url, redis_url, timeout_seconds=settings.dependency_timeout_seconds)
    worker = Worker(settings, store)
    signal.signal(signal.SIGINT, lambda _signum, _frame: worker.stop())
    signal.signal(signal.SIGTERM, lambda _signum, _frame: worker.stop())
    try:
        worker.run()
    finally:
        store.close()


if __name__ == "__main__":
    main()
