import inspect
import sys
import types
from pathlib import Path

import pytest

from trading_system.config import Settings
from trading_system.groww import GrowwError, GrowwFeedClient, GrowwReadClient


def order(number, segment="CASH"):
    return {
        "groww_order_id": f"ORDER-{number}",
        "trading_symbol": "NIFTY26AUGFUT",
        "order_status": "EXECUTED",
        "quantity": 75,
        "price": 22400,
        "segment": segment,
        "created_at": "2026-08-10T09:15:22Z",
    }


def trade(number):
    return {
        "price": 22400,
        "quantity": 1,
        "groww_trade_id": f"TRADE-{number}",
        "exchange_trade_id": f"EXCHANGE-{number}",
        "trade_date_time": "2026-08-10T09:16:22Z",
    }


class PaginatedSDK:
    def __init__(self):
        self.order_calls = []
        self.fill_calls = []

    def get_order_list(self, *, page, page_size, segment, timeout):
        self.order_calls.append((segment, page, page_size, timeout))
        if segment != "CASH":
            return {"order_list": []}
        rows = [order(i) for i in range(25)] if page == 0 else [order(25), order(26)]
        return {"order_list": rows}

    def get_trade_list_for_order(self, *, groww_order_id, segment, page, page_size, timeout):
        self.fill_calls.append((groww_order_id, segment, page, page_size, timeout))
        if groww_order_id != "ORDER-0":
            return {"trade_list": []}
        rows = [trade(i) for i in range(50)] if page == 0 else [trade(50)]
        return {"trade_list": rows}


def test_orders_and_fills_exhaust_pages_from_zero_with_effective_sizes():
    sdk = PaginatedSDK()
    orders = GrowwReadClient(sdk).orders_and_fills()

    assert len(orders) == 27
    assert len(orders[0].fills) == 51
    assert sdk.order_calls[:2] == [("CASH", 0, 25, 5), ("CASH", 1, 25, 5)]
    assert ("FNO", 0, 25, 5) in sdk.order_calls
    assert ("COMMODITY", 0, 25, 5) in sdk.order_calls
    assert sdk.fill_calls[:2] == [
        ("ORDER-0", "CASH", 0, 50, 5),
        ("ORDER-0", "CASH", 1, 50, 5),
    ]


def test_repeated_full_order_page_fails_closed():
    class RepeatedOrders(PaginatedSDK):
        def get_order_list(self, *, page, page_size, segment, timeout):
            return {"order_list": [order(i, segment) for i in range(25)]}

    with pytest.raises(GrowwError) as raised:
        GrowwReadClient(RepeatedOrders()).orders_and_fills()

    assert raised.value.detail.code == "GROWW_CONTRACT_MISMATCH"


def test_repeated_full_fill_page_fails_closed():
    class RepeatedFills(PaginatedSDK):
        def get_order_list(self, *, page, page_size, segment, timeout):
            return {"order_list": [order(0)]} if segment == "CASH" else {"order_list": []}

        def get_trade_list_for_order(self, **kwargs):
            return {"trade_list": [trade(i) for i in range(50)]}

    with pytest.raises(GrowwError) as raised:
        GrowwReadClient(RepeatedFills()).orders_and_fills()

    assert raised.value.detail.code == "GROWW_CONTRACT_MISMATCH"


def test_sdk_timeout_is_bounded_and_mapped_safely():
    class GrowwAPITimeoutException(Exception):
        pass

    class TimedOut(PaginatedSDK):
        def get_order_list(self, **kwargs):
            assert kwargs["timeout"] == 5
            raise GrowwAPITimeoutException

    with pytest.raises(GrowwError) as raised:
        GrowwReadClient(TimedOut()).orders_and_fills()

    assert raised.value.detail.code == "GROWW_TIMEOUT"


def test_authentication_uses_exact_totp_flow_without_secret_errors(monkeypatch):
    calls = {}

    class TOTP:
        def __init__(self, secret):
            calls["secret"] = secret

        def now(self):
            return "123456"

    class API:
        def __init__(self, token):
            calls["access_token"] = token

        @staticmethod
        def get_access_token(*, api_key, totp):
            calls["auth"] = (api_key, totp)
            return "access-value"

    import trading_system.groww as groww

    monkeypatch.setattr(groww.pyotp, "TOTP", TOTP)
    monkeypatch.setattr(groww, "_load_sdk", lambda: (API, object))
    client = GrowwReadClient.authenticate(
        Settings(groww_totp_token="token-value", groww_totp_secret="secret-value")
    )

    assert isinstance(client, GrowwReadClient)
    assert calls == {
        "secret": "secret-value",
        "auth": ("token-value", "123456"),
        "access_token": "access-value",
    }

    class FailingAPI(API):
        @staticmethod
        def get_access_token(*, api_key, totp):
            raise RuntimeError(f"raw {api_key} secret-value {totp}")

    monkeypatch.setattr(groww, "_load_sdk", lambda: (FailingAPI, object))
    with pytest.raises(GrowwError) as raised:
        GrowwReadClient.authenticate(
            Settings(groww_totp_token="token-value", groww_totp_secret="secret-value")
        )

    rendered = str(raised.value)
    assert "token-value" not in rendered and "secret-value" not in rendered
    assert raised.value.detail.correlation_id


def test_unreviewed_sdk_version_fails_closed(monkeypatch):
    import trading_system.groww as groww

    monkeypatch.setattr(groww.metadata, "version", lambda package: "1.5.1")
    with pytest.raises(GrowwError) as raised:
        GrowwReadClient(object())
    assert raised.value.detail.code == "GROWW_SDK_VERSION_UNSUPPORTED"


def test_naive_broker_order_time_is_explicitly_assumed_ist():
    from trading_system.groww import _datetime, _timezone_was_assumed

    raw = "2026-08-17T14:00:00"
    assert _datetime(raw).isoformat() == "2026-08-17T08:30:00+00:00"
    assert _timezone_was_assumed(raw)


def test_feed_credentials_are_scoped_private_and_cleaned(monkeypatch, tmp_path):
    import trading_system.groww as groww

    files = types.ModuleType("growwapi.common.files")

    def original(file="original"):
        return "original"

    files.get_cwd = original
    package = types.ModuleType("growwapi")
    common = types.ModuleType("growwapi.common")
    common.files = files
    package.common = common
    monkeypatch.setitem(sys.modules, "growwapi", package)
    monkeypatch.setitem(sys.modules, "growwapi.common", common)
    monkeypatch.setitem(sys.modules, "growwapi.common.files", files)

    class Feed:
        def __init__(self, client):
            directory = Path(files.get_cwd())
            (directory / "a.creds").write_text("jwt", encoding="utf-8")
            (directory / "b.creds").write_text("seed", encoding="utf-8")

        def consume(self):
            pass

    monkeypatch.setattr(groww, "_load_sdk", lambda: (object, Feed))
    credential_dir = tmp_path / "worker-only"

    with pytest.raises(RuntimeError, match="worker stopped"):
        with GrowwReadClient(object()).open_feed(credential_dir) as feed:
            assert isinstance(feed, GrowwFeedClient)
            assert files.get_cwd() == str(credential_dir)
            assert (credential_dir / "a.creds").exists()
            raise RuntimeError("worker stopped")

    assert files.get_cwd is original
    assert not (credential_dir / "a.creds").exists()
    assert not (credential_dir / "b.creds").exists()
    if sys.platform != "win32":
        assert credential_dir.stat().st_mode & 0o077 == 0


def test_public_surface_has_no_execution_or_sdk_escape_hatch():
    forbidden = {
        "place_order",
        "modify_order",
        "cancel_order",
        "create_smart_order",
        "client",
        "sdk",
        "call",
    }
    public = {
        name
        for cls in (GrowwReadClient, GrowwFeedClient)
        for name, _ in inspect.getmembers(cls)
        if not name.startswith("_")
    }
    assert forbidden.isdisjoint(public)

    source = Path(inspect.getfile(GrowwReadClient)).read_text(encoding="utf-8")
    assert "tests/contract/fixtures" not in source.replace("\\", "/")
