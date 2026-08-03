"""Secret-safe TOTP authentication for the Groww SDK."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeAlias
from uuid import uuid4

import pyotp
from growwapi import GrowwAPI  # type: ignore[import-untyped]
from growwapi.groww.exceptions import (  # type: ignore[import-untyped]
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIException,
    GrowwAPIRateLimitException,
    GrowwAPITimeoutException,
)
from requests.exceptions import Timeout as RequestsTimeout

from trading_system.brokers.ports import BrokerUnavailable
from trading_system.settings import Settings


class _Totp(Protocol):
    def now(self) -> str: ...


TotpFactory: TypeAlias = Callable[[str], _Totp]
AccessTokenFactory: TypeAlias = Callable[..., str]
ClientFactory: TypeAlias = Callable[[str], GrowwAPI]


_SAFE_FAILURES: tuple[tuple[type[Exception], str, str], ...] = (
    (GrowwAPIAuthenticationException, "GROWW_AUTH_FAILED", "Groww authentication failed"),
    (GrowwAPIRateLimitException, "GROWW_RATE_LIMITED", "Groww rate limit exceeded"),
    (GrowwAPITimeoutException, "GROWW_TIMEOUT", "Groww request timed out"),
    (RequestsTimeout, "GROWW_TIMEOUT", "Groww request timed out"),
    (TimeoutError, "GROWW_TIMEOUT", "Groww request timed out"),
    (GrowwAPIAuthorisationException, "GROWW_PERMISSION_DENIED", "Groww permission denied"),
)


def map_groww_failure(error: Exception, correlation_id: str) -> BrokerUnavailable:
    """Map an upstream failure without retaining its message or payload."""
    for error_type, code, summary in _SAFE_FAILURES:
        if isinstance(error, error_type):
            return BrokerUnavailable(code=code, correlation_id=correlation_id, summary=summary)
    return BrokerUnavailable(
        code="GROWW_CONTRACT_MISMATCH",
        correlation_id=correlation_id,
        summary="Groww returned an unexpected response",
    )


class GrowwAuthenticator:
    """Create an SDK client while keeping all credentials inside the worker boundary."""

    def __init__(
        self,
        *,
        totp_factory: TotpFactory = pyotp.TOTP,
        access_token_factory: AccessTokenFactory = GrowwAPI.get_access_token,
        client_factory: ClientFactory = GrowwAPI,
        correlation_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._totp_factory = totp_factory
        self._access_token_factory = access_token_factory
        self._client_factory = client_factory
        self._correlation_id_factory = correlation_id_factory

    def create_client(self, settings: Settings) -> GrowwAPI:
        """Exchange configured TOTP credentials for an authenticated client."""
        correlation_id = self._correlation_id_factory()
        token = settings.groww_totp_token
        secret = settings.groww_totp_secret
        if token is None or secret is None:
            raise BrokerUnavailable(
                code="GROWW_AUTH_FAILED",
                correlation_id=correlation_id,
                summary="Groww authentication failed",
            )

        try:
            code = self._totp_factory(secret.get_secret_value()).now()
            access_token = self._access_token_factory(
                api_key=token.get_secret_value(),
                totp=code,
            )
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("access token response is malformed")
            return self._client_factory(access_token)
        except BrokerUnavailable:
            raise
        except Exception as error:
            if type(error) is GrowwAPIException:
                raise BrokerUnavailable(
                    code="GROWW_AUTH_FAILED",
                    correlation_id=correlation_id,
                    summary="Groww authentication failed",
                ) from None
            raise map_groww_failure(error, correlation_id) from None
