from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trading_system.persistence.redis_state import QuoteState, SubscriptionState


def test_live_state_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        QuoteState(instrument_key="NSE:NIFTY50", observed_at=datetime(2026, 8, 3), payload={})


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "api_key",
        "API-Key",
        "api.key",
        "apiKey",
        "api_token",
        "auth-token",
        "access token",
        "clientSecret",
        "totp_token",
        "TOTP.Secret",
        "password",
        "credentials",
        "sdk-client",
        "sdkClient",
        "execution_state",
        "orders",
    ],
)
def test_live_state_recursively_rejects_credential_aliases_without_rendering_secret(
    forbidden_key: str,
) -> None:
    sentinel = "SUPER_PRIVATE_SENTINEL"
    with pytest.raises(ValidationError, match="forbidden live-state field") as caught:
        QuoteState(
            instrument_key="NSE:NIFTY50",
            observed_at=datetime(2026, 8, 3, tzinfo=UTC),
            payload={"outer": [{"nested": {forbidden_key: sentinel}}]},
        )
    assert sentinel not in str(caught.value)


def test_market_identifier_tokens_remain_allowed() -> None:
    value = QuoteState(
        instrument_key="NSE:NIFTY50",
        observed_at=datetime(2026, 8, 3, tzinfo=UTC),
        payload={"instrument_token": "NIFTY50", "exchange_token": "26000"},
    )

    assert value.payload["instrument_token"] == "NIFTY50"


def test_live_state_rejects_non_json_client_objects_without_rendering_them() -> None:
    class SdkClientSentinel:
        def __repr__(self) -> str:
            return "CLIENT_REPR_SENTINEL"

    with pytest.raises(ValidationError) as caught:
        QuoteState(
            instrument_key="NSE:NIFTY50",
            observed_at=datetime(2026, 8, 3, tzinfo=UTC),
            payload={"provider": SdkClientSentinel()},
        )
    assert "CLIENT_REPR_SENTINEL" not in str(caught.value)


def test_subscription_state_is_schema_versioned() -> None:
    value = SubscriptionState(
        subscription_key="market-feed",
        observed_at=datetime(2026, 8, 3, tzinfo=UTC),
        payload={"instruments": ["NSE:NIFTY50"]},
    )

    assert value.schema_version == 1
    assert value.model_dump(mode="json")["observed_at"] == "2026-08-03T00:00:00Z"
