import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr
from redis import Redis

from trading_system.models import Candle, DataSource, EventOutcome, HealthState, PersistedEvent
from trading_system.store import Store

pytestmark = pytest.mark.integration

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL") or os.getenv("TEST_DATABASE_URL")
if not POSTGRES_URL and os.getenv("TEST_POSTGRES_HOST"):
    POSTGRES_URL = (
        f"postgresql://{quote(os.getenv('TEST_POSTGRES_USER', 'trading_system'))}:"
        f"{quote(os.getenv('TEST_POSTGRES_PASSWORD', ''))}@{os.environ['TEST_POSTGRES_HOST']}:"
        f"{os.getenv('TEST_POSTGRES_PORT', '5432')}/{quote(os.getenv('TEST_POSTGRES_DB', 'trading_system'))}"
    )
REDIS_URL = os.getenv("TEST_REDIS_URL")
if not REDIS_URL and os.getenv("TEST_REDIS_HOST"):
    REDIS_URL = (
        f"redis://:{quote(os.getenv('TEST_REDIS_PASSWORD', ''))}@{os.environ['TEST_REDIS_HOST']}:"
        f"{os.getenv('TEST_REDIS_PORT', '6379')}/0"
    )

if not POSTGRES_URL or not REDIS_URL:
    pytest.skip(
        "Step 3 BLOCKED: set explicit TEST_POSTGRES_URL and TEST_REDIS_URL for real services",
        allow_module_level=True,
    )


def test_store_is_idempotent_utc_rebuildable_and_secret_safe() -> None:
    migration = Path(__file__).parents[2] / "migrations" / "001_initial.sql"
    with psycopg.connect(POSTGRES_URL) as connection:
        connection.execute(migration.read_text(encoding="utf-8"))

    run = uuid4().hex
    source = f"integration-test:{run}"
    instrument = f"NIFTY-{run}"
    event_key = f"observation:{run}"
    late_key = f"late:{run}"
    bucket = datetime(2026, 8, 10, 4, 30, tzinfo=timezone.utc)
    observed = bucket.astimezone(timezone(timedelta(hours=5, minutes=30)))
    store = Store(POSTGRES_URL, REDIS_URL)
    redis = Redis.from_url(REDIS_URL, decode_responses=True, protocol=2)
    quote_key = f"trading-system:v1:quote:{instrument}"
    candle_key = f"trading-system:v1:candle:{instrument}:1m"

    try:
        assert store.append_event(
            kind="market_observation",
            source=source,
            idempotency_key=event_key,
            event_at=observed,
            received_at=observed,
            raw={"ltp": "24500.10"},
            normalized={"instrument": instrument, "price": "24500.10", "source_at": observed},
        )
        assert not store.append_event(
            kind="market_observation",
            source=source,
            idempotency_key=event_key,
            event_at=observed,
            received_at=observed,
            raw={"ltp": "24500.10"},
            normalized={"instrument": instrument, "price": "24500.10", "source_at": observed},
        )
        assert store.append_event(
            kind="feed_event",
            source=source,
            idempotency_key=late_key,
            event_at=observed - timedelta(minutes=1),
            received_at=observed,
            raw={"ltp": "24499.90"},
            normalized={"instrument": instrument, "price": "24499.90"},
            late=True,
        )
        events = store.read_events(source=source)
        assert len(events) == 2
        assert any(event["late"] for event in events)
        assert all(event["event_at"].utcoffset() == timedelta(0) for event in events)
        assert any(event["raw"] == {"ltp": "24500.10"} for event in events)
        batch = tuple(
            PersistedEvent(
                kind="feed_depth",
                source=source,
                idempotency_key=f"batch:{run}:{offset}",
                event_identity=f"batch:{run}:{offset}",
                event_at=observed,
                received_at=observed,
                processed_at=observed,
                raw={},
                normalized={"instrument": instrument},
                outcome=EventOutcome.ACCEPTED,
                feed_generation="integration-generation",
            )
            for offset in range(2)
        )
        assert store.append_events(batch) == {item.idempotency_key for item in batch}
        assert store.append_events(batch) == set()

        candle = Candle(
            instrument=instrument,
            interval="1m",
            bucket_start=bucket,
            open=Decimal("24500.10"),
            high=Decimal("24501.00"),
            low=Decimal("24499.90"),
            close=Decimal("24500.50"),
            source=DataSource.LOCAL_RECONSTRUCTED,
            observation_ids=(event_key,),
        )
        assert store.upsert_candle(candle)
        assert not store.upsert_candle(candle)
        assert store.read_candles(instrument, "1m") == [candle]
        live_candle = candle.model_copy(update={"source": DataSource.LIVE_ACCEPTED})
        assert store.upsert_candle(live_candle)
        assert store.read_candles(instrument, "1m") == [live_candle]
        with psycopg.connect(POSTGRES_URL) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM candles WHERE instrument = %s", (instrument,)
                ).fetchone()[0]
                == 2
            )

        assert store.record_health_transition(
            subsystem="store-test",
            state=HealthState.HEALTHY,
            observed_at=observed,
            idempotency_key=run,
        )
        assert not store.record_health_transition(
            subsystem="store-test",
            state=HealthState.HEALTHY,
            observed_at=observed,
            idempotency_key=run,
        )

        store.set_live_value("quote", {"instrument": instrument}, identifier=instrument)
        assert store.get_live_value("quote", identifier=instrument) == {"instrument": instrument}
        assert 0 < redis.ttl(quote_key) <= 3600
        with pytest.raises(ValueError, match="secrets cannot be stored"):
            store.set_live_value("account", {"value": SecretStr("must-not-persist")})
        with pytest.raises(ValueError, match="accessToken"):
            store.set_live_value("account", {"accessToken": "must-not-persist"})
        assert store.append_event(
            kind="instrument_import",
            source=source,
            idempotency_key=f"instrument:{run}",
            event_at=observed,
            received_at=observed,
            raw={"exchange_token": "instrument-identity"},
            normalized={"exchange_token": "instrument-identity"},
        )

        redis.delete(quote_key, candle_key)
        assert store.get_live_value("quote", identifier=instrument) is None
        assert store.rebuild_live_state() >= 2
        assert store.get_live_value("quote", identifier=instrument)["instrument"] == instrument
        assert (
            store.get_live_value("candle", identifier=instrument, interval="1m")["instrument"]
            == instrument
        )
        assert 0 < redis.ttl(candle_key) <= 600
    finally:
        with psycopg.connect(POSTGRES_URL) as connection:
            connection.execute("DELETE FROM health_transitions WHERE idempotency_key = %s", (run,))
            connection.execute("DELETE FROM candles WHERE instrument = %s", (instrument,))
            connection.execute("DELETE FROM events WHERE source = %s", (source,))
        redis.delete(quote_key, candle_key)
        redis.close()
        store.close()
