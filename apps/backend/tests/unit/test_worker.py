from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import threading

import pytest
import trading_system.worker as worker_module
from trading_system.config import Settings
from trading_system.models import (
    AccountSummary,
    DataSource,
    Instrument,
    NiftySnapshot,
    OptionSnapshot,
    OptionWindow,
    Quote,
)
from trading_system.groww import GrowwError
from trading_system.worker import (
    Worker,
    best_prices,
    bounded_backoff,
    broker_failure_blocks,
    feed_datetime,
    feed_is_silent,
    feed_problem_demotes,
)

UTC = timezone.utc


def instrument(symbol, kind, *, strike=None, option_type=None):
    return Instrument(
        trading_symbol=symbol,
        exchange_token=symbol,
        exchange="NSE",
        segment="CASH" if kind == "INDEX" else "FNO",
        instrument_type=kind,
        underlying_symbol="NIFTY",
        expiry=date(2026, 8, 13) if kind in {"OPTIDX", "FUTIDX"} else None,
        strike=strike,
        option_type=option_type,
    )


SPOT = instrument("NIFTY", "INDEX")
FUTURE = instrument("NIFTY-FUT", "FUTIDX")
OPTIONS = tuple(
    instrument(f"NIFTY-{strike}-{side}", "OPTIDX", strike=Decimal(str(strike)), option_type=side)
    for strike in (22000, 22100, 22200)
    for side in ("CE", "PE")
)
INSTRUMENTS = (SPOT, FUTURE, *OPTIONS)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class MemoryStore:
    def __init__(self, log=None):
        self.log = log if log is not None else []
        self.live = {}
        self.events = {}
        self.candles = []
        self.fail_health = False

    def rebuild_live_state(self):
        self.log.append("stores")

    def healthcheck(self):
        if self.fail_health:
            raise RuntimeError("store unavailable")
        self.log.append("healthcheck")

    def set_live_value(self, kind, value, *, identifier=None, interval=None):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="python")
        self.live[(kind, identifier, interval)] = value

    def get_live_value(self, kind, *, identifier=None, interval=None):
        return self.live.get((kind, identifier, interval))

    def append_event(self, **values):
        key = (values["source"], values["idempotency_key"])
        if key in self.events:
            return False
        self.events[key] = values
        self.log.append(values["kind"])
        return True

    def upsert_candle(self, candle):
        self.candles.append(candle)
        return True


class Client:
    def __init__(self, log=None):
        self.log = log if log is not None else []
        self.account_calls = self.market_calls = 0
        self.audit_raw = {
            "instruments": {"rows": [{"trading_symbol": "NIFTY"}]},
            "market_snapshot": {
                "spot_quote": {"last_price": 22110},
                "option_chain": {"strikes": {}},
            },
            "account_snapshot": {"margin": {"clear_cash": 1000}},
            "orders_and_fills": {"order_pages": []},
        }

    def instruments(self):
        self.log.append("instruments")
        return INSTRUMENTS

    def spot_quote(self, instrument, observed_at):
        self.log.append("spot")
        return Quote(
            instrument=instrument,
            price=Decimal("22110"),
            source=DataSource.GROWW_LIVE,
            source_at=observed_at,
        )

    def market_snapshot(self, universe, observed_at):
        self.log.append("market")
        self.market_calls += 1
        return (
            NiftySnapshot(
                spot=Quote(
                    instrument=universe.spot,
                    price=Decimal("22110"),
                    source=DataSource.GROWW_LIVE,
                    source_at=observed_at,
                ),
                future=Quote(
                    instrument=universe.future,
                    price=Decimal("22150"),
                    source=DataSource.GROWW_LIVE,
                    source_at=observed_at,
                ),
            ),
            OptionWindow(
                expiry=universe.option_expiry,
                atm_strike=universe.atm_strike,
                contracts=tuple(
                    OptionSnapshot(
                        instrument=item, source=DataSource.GROWW_LIVE, source_at=observed_at
                    )
                    for item in universe.options
                ),
                source_at=observed_at,
            ),
        )

    def account_snapshot(self, observed_at):
        self.log.append("account")
        self.account_calls += 1
        return AccountSummary(source_at=observed_at)

    def orders_and_fills(self):
        self.log.append("orders")
        return ()


class Feed:
    def __init__(self):
        self.calls = []
        self.values = {}
        self.fail_depth = False
        self.fail_unsubscribe = None

    def __getattr__(self, name):
        if name.startswith("subscribe_") or name.startswith("unsubscribe_"):

            def call(items, callback=None):
                self.calls.append((name, tuple(item.trading_symbol for item in items)))
                if name == "subscribe_market_depth" and self.fail_depth:
                    raise RuntimeError("depth unavailable")
                if name == self.fail_unsubscribe:
                    raise RuntimeError("unsubscribe unavailable")

            return call
        if name.startswith("get_"):
            return lambda: self.values[name]
        raise AttributeError(name)


def make_worker(clock=None):
    clock = clock or Clock(datetime(2026, 8, 10, 4, 1, tzinfo=UTC))
    store = MemoryStore()
    worker = Worker(
        Settings(groww_totp_token="token", groww_totp_secret="secret"),
        store,
        clock=clock,
        jitter=lambda: 0,
    )
    worker.instruments = INSTRUMENTS
    worker.universe = __import__(
        "trading_system.market", fromlist=["resolve_nifty_universe"]
    ).resolve_nifty_universe(INSTRUMENTS, Decimal("22110"), date(2026, 8, 10))
    worker._instrument_day = clock().date()
    worker._session_open = True
    return worker, store, clock


def test_startup_order_and_readiness_waits_for_feed() -> None:
    log = []
    store, client = MemoryStore(log), Client(log)
    worker = Worker(
        Settings(groww_totp_token="token", groww_totp_secret="secret"),
        store,
        authenticate=lambda settings: (log.append("auth"), client)[1],
        clock=Clock(datetime(2026, 8, 10, 4, 0, tzinfo=UTC)),
    )
    worker.startup()

    assert log.index("stores") < log.index("auth") < log.index("instruments")
    assert log.index("spot") < log.index("market")
    assert log.index("account") < log.index("account_snapshot")
    assert log.index("orders") < log.index("account_snapshot")
    assert store.events[
        ("GROWW", next(key[1] for key in store.events if key[1].startswith("market:")))
    ]["raw"]
    assert not worker.readiness.is_ready

    feed = Feed()
    worker.start_feed(feed)
    assert not worker.readiness.is_ready
    feed.values["get_index_value"] = {
        "NSE": {"CASH": {"NIFTY": {"value": 22110, "tsInMillis": 1786334400000}}}
    }
    worker._index_callback({"exchange": "NSE", "segment": "CASH", "feed_key": "NIFTY"})
    assert worker.readiness.is_ready


def test_startup_configuration_failure_publishes_safe_blocked_reason() -> None:
    store = MemoryStore()
    worker = Worker(Settings(_env_file=None), store)
    with pytest.raises(RuntimeError):
        worker.run()
    heartbeat = store.live[("heartbeat", "feed", None)]
    assert heartbeat["state"] == "BLOCKED" and heartbeat["reason"] == "GROWW_CONFIG_MISSING"


def test_incomplete_required_option_window_fails_closed() -> None:
    worker, _, clock = make_worker()
    market, _ = Client().market_snapshot(worker.universe, clock())
    with pytest.raises(GrowwError):
        worker._store_market(
            market,
            OptionWindow(
                expiry=worker.universe.option_expiry,
                atm_strike=worker.universe.atm_strike,
                contracts=(),
                source_at=clock(),
            ),
        )


def test_callback_deduplicates_and_audits_late_event() -> None:
    clock = Clock(datetime(2026, 8, 10, 4, 0, 5, tzinfo=UTC))
    worker, store, _ = make_worker(clock)
    feed = Feed()
    worker.feed = feed
    meta = {"exchange": "NSE", "segment": "CASH", "feed_key": "NIFTY"}
    feed.values["get_index_value"] = {
        "NSE": {"CASH": {"NIFTY": {"value": 100, "tsInMillis": 1786334405000}}}
    }
    worker._index_callback(meta)
    first_quote = store.live[("quote", "NIFTY", None)]
    worker._index_callback(meta)
    clock.now = datetime(2026, 8, 10, 4, 1, tzinfo=UTC)
    worker._finalize(clock())
    feed.values["get_index_value"]["NSE"]["CASH"]["NIFTY"] = {
        "value": 99,
        "tsInMillis": 1786334440000,
    }
    worker._index_callback(meta)

    observations = [
        event for event in store.events.values() if event["kind"] == "market_observation"
    ]
    assert [str(event["outcome"]) for event in observations] == ["ACCEPTED", "DUPLICATE", "STALE"]
    assert store.live[("quote", "NIFTY", None)] == first_quote


def test_future_feed_events_are_audited_but_never_promoted_live() -> None:
    clock = Clock(datetime(2026, 8, 10, 4, 0, 5, tzinfo=UTC))
    worker, store, _ = make_worker(clock)
    worker._session_open = True
    worker.feed = Feed()
    option = OPTIONS[0]
    timestamp = int((clock() + timedelta(minutes=2)).timestamp() * 1000)
    meta = {"exchange": "NSE", "segment": "FNO", "feed_key": option.exchange_token}
    worker.feed.values["get_ltp"] = {
        "NSE": {"FNO": {option.exchange_token: {"ltp": 101, "tsInMillis": timestamp}}}
    }
    worker._ltp_callback(meta)
    worker.feed.values["get_market_depth"] = {
        "NSE": {
            "FNO": {
                option.exchange_token: {
                    "buyBook": [{"value": {"price": 100}}],
                    "sellBook": [{"value": {"price": 102}}],
                    "tsInMillis": timestamp,
                }
            }
        }
    }
    worker._depth_callback(meta)

    assert worker.readiness.first_feed is False
    assert ("quote", option.trading_symbol, None) not in store.live
    assert store.live[("heartbeat", "feed", None)]["reason"] == "FUTURE_TIMESTAMP"
    assert [event["late"] for event in store.events.values()] == [True, True]
    worker._finalize(clock() + timedelta(minutes=5))
    assert store.candles == []


def test_closed_session_feed_event_is_audited_without_live_state() -> None:
    clock = Clock(datetime(2026, 8, 10, 4, 0, 5, tzinfo=UTC))
    worker, store, _ = make_worker(clock)
    worker._session_open = False
    worker.feed = Feed()
    meta = {"exchange": "NSE", "segment": "CASH", "feed_key": "NIFTY"}
    worker.feed.values["get_index_value"] = {
        "NSE": {"CASH": {"NIFTY": {"value": 100, "tsInMillis": int(clock().timestamp() * 1000)}}}
    }
    worker._index_callback(meta)
    assert ("quote", "NIFTY", None) not in store.live
    assert next(iter(store.events.values()))["late"] is True


def test_inactive_generation_is_recorded_without_touching_old_feed_getter() -> None:
    worker, store, _ = make_worker()
    worker.feed = Feed()

    def forbidden_read():
        raise AssertionError("inactive feed getter must not be called")

    worker._price_callback(
        {"exchange": "NSE", "segment": "CASH", "feed_key": "NIFTY"},
        forbidden_read,
        "value",
        Feed(),
    )
    event = next(iter(store.events.values()))
    assert str(event["outcome"]) == "INACTIVE_FEED_GENERATION"
    assert ("quote", "NIFTY", None) not in store.live


def test_depth_uses_price_extrema_and_preserves_missing_side() -> None:
    assert best_prices(
        {
            "buyBook": [{"value": {"price": 10}}, {"value": {"price": 12}}],
            "sellBook": [{"value": {"price": 15}}, {"value": {"price": 14}}],
        }
    ) == (Decimal("12"), Decimal("14"))
    assert best_prices({"buyBook": [{"value": {"price": 12}}], "sellBook": []}) == (
        Decimal("12"),
        None,
    )

    worker, store, _ = make_worker(Clock(datetime(2026, 8, 10, 4, 0, 5, tzinfo=UTC)))
    worker.feed = Feed()
    option = OPTIONS[0]
    worker.feed.values["get_market_depth"] = {
        "NSE": {
            "FNO": {
                option.exchange_token: {
                    "buyBook": [{"value": {"price": 12}}],
                    "sellBook": [],
                    "tsInMillis": 1786334405000,
                }
            }
        }
    }
    worker._depth_callback({"exchange": "NSE", "segment": "FNO", "feed_key": option.exchange_token})
    value = store.live[("option-contract", option.trading_symbol, None)]
    assert value["bid"]["value"] == Decimal("12")
    assert value["ask"]["value"] is None and value["depth"]["value"]["spread"] is None
    assert store.live[("heartbeat", "depth", None)]["state"] == "DEGRADED"


def test_sixty_second_refresh_backoff_swap_rollback_and_shutdown() -> None:
    worker, _, clock = make_worker()
    worker.client = Client()
    worker._last_account = worker._last_options = clock()
    clock.now += timedelta(seconds=59)
    worker.tick()
    assert worker.client.account_calls == 0
    clock.now += timedelta(seconds=1)
    worker.tick()
    assert worker.client.account_calls == 1 and worker.client.market_calls == 1
    assert list(next(bounded_backoff(lambda: 0)) for _ in range(1)) == [1]

    feed = Feed()
    worker.feed = feed
    old = worker.universe
    added = instrument("NIFTY-22300-CE", "OPTIDX", strike=Decimal("22300"), option_type="CE")
    new = old.model_copy(
        update={"options": (*old.options[:-2], added), "atm_strike": Decimal("22300")}
    )
    worker.swap_universe(new)
    write_index = next(i for i, call in enumerate(feed.calls) if call[0] == "subscribe_ltp")
    remove_index = next(i for i, call in enumerate(feed.calls) if call[0] == "unsubscribe_ltp")
    assert write_index < remove_index

    worker.universe = old
    feed.calls.clear()
    feed.fail_depth = True
    failed = old.model_copy(
        update={"options": (*old.options[:-4], added), "atm_strike": Decimal("22300")}
    )
    try:
        worker.swap_universe(failed)
    except RuntimeError:
        pass
    assert worker.universe == old
    assert any(call[0] == "unsubscribe_market_depth" for call in feed.calls)

    feed.fail_depth = False
    feed.fail_unsubscribe = "unsubscribe_market_depth"
    worker.stop()
    assert [call[0] for call in feed.calls[-3:]] == [
        "unsubscribe_market_depth",
        "unsubscribe_ltp",
        "unsubscribe_index_value",
    ]


def test_refresh_failure_marks_stale_and_uses_bounded_retry() -> None:
    worker, store, clock = make_worker()

    class FailingClient(Client):
        def account_snapshot(self, observed_at):
            self.account_calls += 1
            raise TimeoutError

    worker.client = FailingClient()
    worker._last_account = worker._last_options = clock() - timedelta(seconds=60)
    worker.tick()
    assert store.live[("heartbeat", "account_reconciliation", None)]["state"] == "STALE"
    assert worker._retry_at == clock() + timedelta(seconds=1)
    worker.tick()
    assert worker.client.account_calls == 1


def test_store_health_loss_blocks_then_rebuilds_and_reconciles_before_recovery() -> None:
    worker, store, clock = make_worker()
    worker.client = Client()
    worker.feed = Feed()
    worker.readiness.stores = worker.readiness.reconciled = True
    worker.readiness.subscriptions = worker.readiness.first_feed = True
    store.fail_health = True
    worker.tick()
    assert worker.readiness.stores is False

    clock.now += timedelta(seconds=10)
    store.fail_health = False
    store.live.clear()
    worker.tick()
    assert (
        worker.readiness.stores and worker.readiness.reconciled and worker.readiness.subscriptions
    )
    assert worker.readiness.first_feed is False
    assert worker.client.account_calls == 1 and worker.client.market_calls == 1
    assert ("subscription", None, None) in store.live


@pytest.mark.parametrize("failure", ["silence", "tick"])
def test_established_feed_failure_exits_for_supervisor_and_cleans_subscription(
    monkeypatch, failure
) -> None:
    class CapturingStore(MemoryStore):
        def __init__(self):
            super().__init__()
            self.heartbeats = []

        def set_live_value(self, kind, value, *, identifier=None, interval=None):
            super().set_live_value(kind, value, identifier=identifier, interval=interval)
            if kind == "heartbeat":
                self.heartbeats.append((identifier, str(value["state"]), value.get("reason")))

    class ConsumingFeed(Feed):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()

        def consume(self):
            self.release.wait(3)

    class OneShotClient(Client):
        def __init__(self):
            super().__init__()
            self.connections = 0
            self.feeds = []

        @contextmanager
        def open_feed(self, _credential_dir):
            self.connections += 1
            feed = ConsumingFeed()
            self.feeds.append(feed)
            try:
                yield feed
            finally:
                feed.release.set()

    if failure == "silence":
        stale_checks = [True]
        monkeypatch.setattr(
            worker_module,
            "feed_is_silent",
            lambda *_args: stale_checks.pop(0) if stale_checks else False,
        )
    store, client = CapturingStore(), OneShotClient()
    worker = Worker(
        Settings(groww_totp_token="token", groww_totp_secret="secret"),
        store,
        authenticate=lambda _settings: client,
        clock=Clock(datetime(2026, 8, 10, 4, 1, tzinfo=UTC)),
        jitter=lambda: 0,
    )
    if failure == "tick":
        worker.tick = lambda: (_ for _ in ()).throw(RuntimeError("tick failed"))
    with pytest.raises((worker_module.FeedRestartRequired, RuntimeError)):
        worker.run()

    assert client.connections == 1
    assert ("feed", "BLOCKED", "FEED_PROCESS_RESTART_REQUIRED") in store.heartbeats
    assert [call[0] for call in client.feeds[0].calls[-3:]] == [
        "unsubscribe_market_depth",
        "unsubscribe_ltp",
        "unsubscribe_index_value",
    ]


def test_silent_feed_requires_process_restart_only_during_open_session() -> None:
    subscribed = datetime(2026, 8, 10, 4, 0, tzinfo=UTC)
    assert feed_is_silent(True, None, subscribed, subscribed + timedelta(seconds=11), 10)
    assert not feed_is_silent(False, None, subscribed, subscribed + timedelta(seconds=11), 10)
    assert broker_failure_blocks(GrowwError("GROWW_PERMISSION_DENIED", "denied"))
    assert not broker_failure_blocks(GrowwError("GROWW_TIMEOUT", "timeout"))


def test_feed_datetime_accepts_seconds_and_milliseconds() -> None:
    expected = datetime(2026, 8, 17, 7, tzinfo=UTC)
    assert feed_datetime(int(expected.timestamp())) == expected
    assert feed_datetime(int(expected.timestamp() * 1000)) == expected


def test_rejected_event_only_demotes_feed_without_recent_accepted_event() -> None:
    now = datetime(2026, 8, 17, 7, tzinfo=UTC)
    assert not feed_problem_demotes("FEED_EVENT_STALE", now, now, 10)
    assert feed_problem_demotes("FEED_EVENT_STALE", now - timedelta(seconds=11), now, 10)
    assert feed_problem_demotes("FEED_EVENT_STALE", None, now, 10)
