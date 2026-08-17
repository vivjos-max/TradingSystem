import json

from trading_system.api import create_app

from test_api import MemoryStore


def test_openapi_is_exactly_the_read_only_allowlist() -> None:
    schema = create_app(MemoryStore()).openapi()
    assert set(schema["paths"]) == {
        "/health/live",
        "/health/ready",
        "/api/v1/system/status",
        "/api/v1/market/nifty",
        "/api/v1/market/nifty/options",
        "/api/v1/market/nifty/candles",
        "/api/v1/account/summary",
        "/api/v1/account/orders",
    }
    for path, item in schema["paths"].items():
        assert set(item) <= {"get", "head", "parameters"}, path

    rendered = json.dumps(schema).lower()
    for forbidden in ("place_order", "modify_order", "cancel_order", "create_order", "order-ticket", "totp", "credential"):
        assert forbidden not in rendered
