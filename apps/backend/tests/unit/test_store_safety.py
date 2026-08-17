import pytest
from unittest.mock import patch

from trading_system.store import Store, _json_value


@pytest.mark.parametrize(
    "key",
    ["accessToken", "apiKey", "refreshToken", "socketToken", "nkeySeed", "secret", "token", "client_secret", "private_key"],
)
def test_store_rejects_secret_bearing_payload_keys(key: str) -> None:
    with pytest.raises(ValueError, match="secret field"):
        _json_value({key: "must-not-persist"})


def test_store_keeps_non_secret_exchange_token() -> None:
    assert _json_value({"exchange_token": "NIFTY", "missing": float("nan")}) == {
        "exchange_token": "NIFTY",
        "missing": None,
    }


def test_store_uses_redis_compatible_protocol() -> None:
    with patch("trading_system.store.Redis.from_url") as from_url:
        Store("postgresql://unused", "redis://unused")

    assert from_url.call_args.kwargs["protocol"] == 2
