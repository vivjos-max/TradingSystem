from __future__ import annotations

from typing import Any

import pytest
from growwapi.groww.exceptions import (
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIException,
    GrowwAPIRateLimitException,
    GrowwAPITimeoutException,
)
from requests.exceptions import Timeout as RequestsTimeout

from trading_system.brokers.groww.auth import GrowwAuthenticator
from trading_system.brokers.ports import BrokerUnavailable
from trading_system.settings import Settings


TOKEN = "unit-token-sensitive"
SECRET = "unit-secret-sensitive"
CODE = "739104"
CORRELATION_ID = "corr-auth-unit-1"


def configured_settings() -> Settings:
    return Settings(
        _env_file=None,
        groww_totp_token=TOKEN,
        groww_totp_secret=SECRET,
    )


def test_create_client_uses_totp_code_to_exchange_token() -> None:
    seen: dict[str, Any] = {}
    expected_client = object()

    class FakeTotp:
        def __init__(self, secret: str) -> None:
            seen["totp_secret"] = secret

        def now(self) -> str:
            return CODE

    def exchange_token(*, api_key: str, totp: str) -> str:
        seen["exchange"] = {"api_key": api_key, "totp": totp}
        return "invented-access-token"

    def create_sdk_client(access_token: str) -> object:
        seen["access_token"] = access_token
        return expected_client

    authenticator = GrowwAuthenticator(
        totp_factory=FakeTotp,
        access_token_factory=exchange_token,
        client_factory=create_sdk_client,
        correlation_id_factory=lambda: CORRELATION_ID,
    )

    assert authenticator.create_client(configured_settings()) is expected_client
    assert seen == {
        "totp_secret": SECRET,
        "exchange": {"api_key": TOKEN, "totp": CODE},
        "access_token": "invented-access-token",
    }


@pytest.mark.parametrize(
    ("sdk_error", "expected_code", "expected_summary"),
    [
        (
            GrowwAPIAuthenticationException(),
            "GROWW_AUTH_FAILED",
            "Groww authentication failed",
        ),
        (
            GrowwAPIRateLimitException(),
            "GROWW_RATE_LIMITED",
            "Groww rate limit exceeded",
        ),
        (
            GrowwAPITimeoutException(),
            "GROWW_TIMEOUT",
            "Groww request timed out",
        ),
        (
            RequestsTimeout(f"timeout {TOKEN} {SECRET} {CODE}"),
            "GROWW_TIMEOUT",
            "Groww request timed out",
        ),
        (
            GrowwAPIAuthorisationException(),
            "GROWW_PERMISSION_DENIED",
            "Groww permission denied",
        ),
        (
            GrowwAPIException(msg=f"bad totp {TOKEN} {SECRET} {CODE}", code="400"),
            "GROWW_AUTH_FAILED",
            "Groww authentication failed",
        ),
        (
            ValueError(f"malformed {TOKEN} {SECRET} {CODE}"),
            "GROWW_CONTRACT_MISMATCH",
            "Groww returned an unexpected response",
        ),
    ],
)
def test_create_client_maps_failures_without_exposing_secrets(
    sdk_error: Exception,
    expected_code: str,
    expected_summary: str,
) -> None:
    class FakeTotp:
        def __init__(self, secret: str) -> None:
            assert secret == SECRET

        def now(self) -> str:
            return CODE

    def fail_exchange(*, api_key: str, totp: str) -> str:
        assert api_key == TOKEN
        assert totp == CODE
        raise sdk_error

    authenticator = GrowwAuthenticator(
        totp_factory=FakeTotp,
        access_token_factory=fail_exchange,
        client_factory=lambda _: object(),
        correlation_id_factory=lambda: CORRELATION_ID,
    )

    with pytest.raises(BrokerUnavailable) as captured:
        authenticator.create_client(configured_settings())

    error = captured.value
    assert error.code == expected_code
    assert error.correlation_id == CORRELATION_ID
    assert error.summary == expected_summary
    assert error.safe_view().model_dump() == {
        "code": expected_code,
        "correlation_id": CORRELATION_ID,
        "summary": expected_summary,
    }
    rendered = " ".join((str(error), repr(error), repr(error.safe_view())))
    assert TOKEN not in rendered
    assert SECRET not in rendered
    assert CODE not in rendered
