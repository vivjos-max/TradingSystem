from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.dialects import postgresql

from trading_system.persistence.postgres import (
    CandleInput,
    _candle_idempotency_key,
    _latest_account_snapshots_statement,
    _latest_broker_snapshots_statement,
    _latest_candles_statement,
    _latest_health_transitions_statement,
    _latest_observations_statement,
)


def _sql(statement: object) -> str:
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return " ".join(str(compiled).split()).upper()


def test_rebuild_reads_select_latest_row_per_replaceable_identity_without_limit() -> None:
    contracts = {
        "MARKET_OBSERVATIONS.INSTRUMENT_KEY": _latest_observations_statement(),
        "BROKER_SNAPSHOTS.SNAPSHOT_KEY": _latest_broker_snapshots_statement("option_chain"),
        "ACCOUNT_SNAPSHOTS.ACCOUNT_KEY": _latest_account_snapshots_statement(),
        "HEALTH_TRANSITIONS.SERVICE": _latest_health_transitions_statement(),
        "CANDLES.INSTRUMENT_KEY, CANDLES.INTERVAL": _latest_candles_statement(
            finalized=False
        ),
    }

    for identity, statement in contracts.items():
        sql = _sql(statement)
        assert "ROW_NUMBER() OVER" in sql
        assert f"PARTITION BY {identity}" in sql
        assert "CREATED_AT DESC" in sql
        assert "ID DESC" in sql
        assert " LIMIT " not in sql


def test_candle_idempotency_key_is_fixed_length_and_hashes_caller_and_natural_key() -> None:
    at = datetime(2026, 8, 3, 9, 15, tzinfo=UTC)
    candle = CandleInput(
        instrument_key="NSE:NIFTY50",
        interval="1m",
        opened_at=at,
        closed_at=at + timedelta(minutes=1),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=None,
        finalized=True,
        is_late=False,
        raw_payload={},
    )
    maximum_caller_key = "x" * 256

    first = _candle_idempotency_key(maximum_caller_key, "groww_live", candle)
    duplicate = _candle_idempotency_key(maximum_caller_key, "groww_live", candle)
    changed_caller = _candle_idempotency_key("y" * 256, "groww_live", candle)

    assert first == duplicate
    assert first != changed_caller
    assert first.startswith("candle:")
    assert len(first) == 71
    assert len(first) <= 256
