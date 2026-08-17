from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from trading_system.config import Settings
from trading_system.groww import GrowwReadClient
from trading_system.market import resolve_nifty_universe


pytestmark = pytest.mark.live
_EXECUTION_WORDS = re.compile(
    r"(?:place|modify|cancel|execute|buy|sell|gtt|oco|smart)[_-]?order", re.I
)


def _get(base_url: str, path: str, *, allow_error: bool = False) -> tuple[int, Any]:
    try:
        with urlopen(
            Request(f"{base_url}{path}", headers={"Accept": "application/json"}), timeout=5
        ) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        if allow_error:
            try:
                return exc.code, json.load(exc)
            finally:
                exc.close()
        raise AssertionError("running API returned an unexpected error") from None
    except (OSError, URLError, ValueError):
        raise AssertionError("running API is unavailable or returned invalid JSON") from None


def _at(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AssertionError("live response timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _same_number(
    direct: Any,
    served: Any,
    *,
    absolute: Decimal,
    relative: Decimal,
) -> bool:
    left, right = _number(direct), _number(served)
    if left is None or right is None:
        return left is right
    return abs(left - right) <= max(absolute, relative * max(abs(left), abs(right)))


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise AssertionError(reason)


@dataclass(frozen=True)
class LiveContext:
    base_url: str
    market: Any
    options: Any
    account: Any
    orders: tuple[Any, ...]
    api_market: dict[str, Any]
    api_options: dict[str, Any]
    api_account: dict[str, Any]
    api_orders: dict[str, Any]


@pytest.fixture(scope="module")
def live() -> LiveContext:
    if os.getenv("LIVE_ACCEPTANCE") != "1":
        pytest.skip("LIVE_ACCEPTANCE=1 was not set; credentialed live acceptance was not requested")

    settings = Settings()
    if not settings.groww_configured:
        pytest.skip("local Groww TOTP credentials are not available")

    base_url = os.getenv("LIVE_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    status, _ = _get(base_url, "/health/live", allow_error=True)
    _require(status == 200, "running API failed its liveness check")

    client = GrowwReadClient.authenticate(settings)
    instruments = client.instruments()
    strikes = sorted({item.strike for item in instruments if item.strike is not None})
    _require(bool(strikes), "live instrument master has no option strikes")
    now = datetime.now(timezone.utc)
    provisional = resolve_nifty_universe(instruments, strikes[len(strikes) // 2], now.date())
    first_market, _ = client.market_snapshot(provisional, now)
    universe = resolve_nifty_universe(instruments, first_market.spot.price, now.date())
    market, options = client.market_snapshot(universe, datetime.now(timezone.utc))
    account = client.account_snapshot(datetime.now(timezone.utc))
    orders = client.orders_and_fills()

    _, api_market = _get(base_url, "/api/v1/market/nifty")
    _, api_options = _get(base_url, "/api/v1/market/nifty/options")
    _, api_account = _get(base_url, "/api/v1/account/summary")
    _, api_orders = _get(base_url, "/api/v1/account/orders")
    return LiveContext(
        base_url,
        market,
        options,
        account,
        orders,
        api_market,
        api_options,
        api_account,
        api_orders,
    )


def test_direct_groww_matches_running_api(live: LiveContext) -> None:
    timestamp_tolerance = timedelta(
        seconds=int(os.getenv("LIVE_TIMESTAMP_TOLERANCE_SECONDS", "75"))
    )
    price_absolute = Decimal(os.getenv("LIVE_PRICE_ABSOLUTE_TOLERANCE", "5"))
    relative = Decimal(os.getenv("LIVE_VALUE_RELATIVE_TOLERANCE", "0.02"))

    for direct, served in (
        (live.market.spot, live.api_market.get("spot", {})),
        (live.market.future, live.api_market.get("future", {})),
    ):
        _require(direct is not None, "direct Groww future quote is missing")
        _require(
            served.get("instrument", {}).get("trading_symbol") == direct.instrument.trading_symbol,
            "direct and API NIFTY instrument identifiers differ",
        )
        _require(
            abs(_at(served.get("source_at")) - direct.source_at) <= timestamp_tolerance,
            "direct and API NIFTY timestamps exceed tolerance",
        )
        _require(
            _same_number(
                direct.price, served.get("price"), absolute=price_absolute, relative=relative
            ),
            "direct and API NIFTY values exceed tolerance",
        )

    direct_options = {item.instrument.trading_symbol: item for item in live.options.contracts}
    served_options = {
        item.get("instrument", {}).get("trading_symbol"): item
        for item in live.api_options.get("contracts", ())
    }
    _require(set(direct_options) == set(served_options), "direct and API option windows differ")
    _require(
        abs(_at(live.api_options.get("source_at")) - live.options.source_at) <= timestamp_tolerance,
        "direct and API option timestamps exceed tolerance",
    )
    for symbol, direct in direct_options.items():
        served = served_options[symbol]
        for field in ("ltp", "volume", "open_interest"):
            _require(
                _same_number(
                    getattr(direct, field),
                    served.get(field),
                    absolute=price_absolute if field == "ltp" else Decimal(1),
                    relative=relative,
                ),
                "direct and API option values exceed tolerance",
            )


def test_redacted_account_and_order_reconciliation(live: LiveContext) -> None:
    timestamp_tolerance = timedelta(
        seconds=int(os.getenv("LIVE_TIMESTAMP_TOLERANCE_SECONDS", "75"))
    )
    _require(
        abs(_at(live.api_account.get("source_at")) - live.account.source_at) <= timestamp_tolerance,
        "direct and API account timestamps exceed tolerance",
    )
    for field in ("available_margin", "used_margin"):
        _require(
            _same_number(
                getattr(live.account, field),
                live.api_account.get(field),
                absolute=Decimal("0.01"),
                relative=Decimal("0.000001"),
            ),
            "direct and API redacted account reconciliation differs",
        )
    direct_positions = {(item.trading_symbol, item.quantity) for item in live.account.positions}
    served_positions = {
        (str(item.get("trading_symbol")), Decimal(str(item.get("quantity"))))
        for item in live.api_account.get("positions", ())
    }
    _require(direct_positions == served_positions, "direct and API redacted positions differ")

    direct_orders = {
        (item.order_id, item.segment, item.status, item.quantity, len(item.fills))
        for item in live.orders
    }
    served_orders = {
        (
            str(item.get("order_id")),
            str(item.get("segment")),
            str(item.get("status")),
            Decimal(str(item.get("quantity"))),
            len(item.get("fills", ())),
        )
        for item in live.api_orders.get("orders", ())
    }
    _require(direct_orders == served_orders, "direct and API redacted order reconciliation differs")


def test_running_openapi_is_get_only(live: LiveContext) -> None:
    _, schema = _get(live.base_url, "/openapi.json")
    allowed_paths = {
        "/health/live",
        "/health/ready",
        "/api/v1/system/status",
        "/api/v1/market/nifty",
        "/api/v1/market/nifty/options",
        "/api/v1/market/nifty/candles",
        "/api/v1/account/summary",
        "/api/v1/account/orders",
    }
    _require(set(schema.get("paths", {})) == allowed_paths, "OpenAPI path allowlist differs")
    operations = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
    for path, item in schema.get("paths", {}).items():
        _require(not _EXECUTION_WORDS.search(path), "OpenAPI exposes an execution-shaped path")
        _require(
            {name.casefold() for name in item if name.casefold() in operations} <= {"get", "head"},
            "OpenAPI exposes a mutating operation",
        )
    _require(
        not _EXECUTION_WORDS.search(json.dumps(schema.get("components", {}))),
        "OpenAPI exposes an execution-shaped schema",
    )


def test_runtime_did_not_load_contract_fixtures(live: LiveContext) -> None:
    del live
    contract_root = (Path(__file__).parents[1] / "contract").resolve()
    loaded = []
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            try:
                Path(filename).resolve().relative_to(contract_root)
            except (OSError, ValueError):
                continue
            loaded.append(filename)
    _require(not loaded, "a contract test or fixture module was loaded during live acceptance")


def _observations(instrument: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    _require(
        bool(re.fullmatch(r"[A-Za-z0-9_.-]+", instrument)), "unsafe live instrument identifier"
    )
    query = (
        "SELECT json_build_object('price',normalized->>'price','event_at',normalized->>'event_at',"
        "'received_at',normalized->>'received_at','observation_id',normalized->>'observation_id') "
        "FROM events WHERE kind='market_observation' "
        f"AND normalized->>'instrument'='{instrument}' "
        f"AND event_at>='{start.isoformat()}' AND event_at<'{end.isoformat()}' "
        "ORDER BY event_at,received_at,normalized->>'observation_id';"
    )
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "sh",
        "-lc",
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "$1"',
        "psql",
        query,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    _require(result.returncode == 0, "durable observation query failed")
    try:
        return [json.loads(line) for line in result.stdout.splitlines() if line]
    except ValueError:
        raise AssertionError("durable observations returned invalid JSON") from None


def test_candles_match_durable_live_observations(live: LiveContext) -> None:
    _, status = _get(live.base_url, "/api/v1/system/status")
    if status.get("state") == "MARKET_CLOSED":
        pytest.skip("MARKET_CLOSED: feed/candle proof is pending an exchange-open observation")
    _require(
        status.get("state") in {"LIVE", "DEGRADED"},
        "market-hours candle proof requires healthy live state",
    )

    deadline = time.monotonic() + int(os.getenv("LIVE_CANDLE_WAIT_SECONDS", "90"))
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        code, candidate = _get(
            live.base_url, "/api/v1/market/nifty/candles?interval=1m", allow_error=True
        )
        if code == 200 and candidate.get("candles"):
            age = datetime.now(timezone.utc) - _at(candidate["candles"][-1]["bucket_start"])
            if timedelta(minutes=-1) <= age <= timedelta(minutes=3):
                payload = candidate
                break
        time.sleep(2)
    _require(bool(payload), "no complete live minute was available within the acceptance window")

    candle = payload["candles"][-1]
    start = _at(candle["bucket_start"])
    age = datetime.now(timezone.utc) - start
    _require(
        timedelta(minutes=-1) <= age <= timedelta(minutes=3),
        "latest 1m candle is stale or future-dated",
    )
    rows = _observations(candle["instrument"], start, start + timedelta(minutes=1))
    _require(bool(rows), "stored 1m candle has no durable live observations")
    prices = [Decimal(row["price"]) for row in rows]
    expected = (prices[0], max(prices), min(prices), prices[-1])
    actual = tuple(Decimal(str(candle[name])) for name in ("open", "high", "low", "close"))
    _require(actual == expected, "stored 1m OHLC differs from durable live observations")

    five_deadline = time.monotonic() + int(os.getenv("LIVE_FIVE_MINUTE_WAIT_SECONDS", "420"))
    five_payload: dict[str, Any] = {}
    while time.monotonic() < five_deadline:
        code, candidate = _get(
            live.base_url, "/api/v1/market/nifty/candles?interval=5m", allow_error=True
        )
        if code == 200 and candidate.get("candles"):
            age = datetime.now(timezone.utc) - _at(candidate["candles"][-1]["bucket_start"])
            if timedelta(minutes=-1) <= age <= timedelta(minutes=10):
                five_payload = candidate
                break
        time.sleep(2)
    _require(
        bool(five_payload),
        "no fresh complete five-minute live window was available within the acceptance window",
    )
    five = five_payload["candles"][-1]
    five_start = _at(five["bucket_start"])
    five_age = datetime.now(timezone.utc) - five_start
    _require(
        timedelta(minutes=-1) <= five_age <= timedelta(minutes=10),
        "latest 5m candle is stale or future-dated",
    )
    rows = _observations(five["instrument"], five_start, five_start + timedelta(minutes=5))
    minutes: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for offset in range(5):
        minute_start = five_start + timedelta(minutes=offset)
        values = [
            Decimal(row["price"])
            for row in rows
            if minute_start <= _at(row["event_at"]) < minute_start + timedelta(minutes=1)
        ]
        _require(bool(values), "stored 5m candle is missing a durable source minute")
        minutes.append((values[0], max(values), min(values), values[-1]))
    expected_five = (
        minutes[0][0],
        max(row[1] for row in minutes),
        min(row[2] for row in minutes),
        minutes[-1][3],
    )
    actual_five = tuple(Decimal(str(five[name])) for name in ("open", "high", "low", "close"))
    _require(actual_five == expected_five, "stored 5m OHLC differs from durable live observations")


def test_reviewed_sdk_version_is_running(live: LiveContext) -> None:
    del live
    _require(metadata.version("growwapi") == "1.5.0", "unreviewed Groww SDK version is running")
