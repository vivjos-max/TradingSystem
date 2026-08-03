from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trading_system.persistence.redis_state import QuoteState, SubscriptionState


def test_live_state_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        QuoteState(instrument_key="NSE:NIFTY50", observed_at=datetime(2026, 8, 3), payload={})


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "secret"},
        {"nested": {"password": "secret"}},
        {"sdk_client": "object"},
        {"execution_state": "pending"},
        {"orders": []},
    ],
)
def test_live_state_rejects_secrets_clients_and_execution_state(payload: dict) -> None:
    with pytest.raises(ValidationError, match="forbidden live-state field"):
        QuoteState(
            instrument_key="NSE:NIFTY50",
            observed_at=datetime(2026, 8, 3, tzinfo=UTC),
            payload=payload,
        )


def test_subscription_state_is_schema_versioned() -> None:
    value = SubscriptionState(
        subscription_key="market-feed",
        observed_at=datetime(2026, 8, 3, tzinfo=UTC),
        payload={"instruments": ["NSE:NIFTY50"]},
    )

    assert value.schema_version == 1
    assert value.model_dump(mode="json")["observed_at"] == "2026-08-03T00:00:00Z"
