from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_system.domain.models import (
    Instrument,
    MarketDataSource,
    Order,
)
from trading_system.brokers.groww.client import GrowwReadClient
from trading_system.brokers.ports import BrokerReadPort, BrokerUnavailable


FIXTURES = Path(__file__).parents[1] / "contract" / "fixtures"


def fixture(name: str) -> Any:
    with (FIXTURES / name).open(encoding="utf-8") as source:
        return json.load(source, parse_float=Decimal)


def instrument() -> Instrument:
    return Instrument(
        instrument_token="88001",
        symbol="NIFTY26AUG24800CE",
        exchange="NSE",
        instrument_type="CE",
        expiry=datetime(2026, 8, 6, tzinfo=UTC).date(),
        strike=Decimal("24800"),
        option_type="CE",
        lot_size=75,
        tick_size=Decimal("0.05"),
        source=MarketDataSource.GROWW_LIVE,
    )


def test_read_client_exposes_exact_read_surface_and_no_execution_methods() -> None:
    public_methods = {name for name in dir(GrowwReadClient) if not name.startswith("_")}
    port_methods = {name for name in BrokerReadPort.__dict__ if not name.startswith("_")}
    forbidden = {"place_order", "modify_order", "cancel_order", "create_smart_order"}

    assert public_methods == {
        "instruments",
        "nifty_quote",
        "option_chain",
        "greeks",
        "margin",
        "positions",
        "orders",
        "fills",
    }
    assert port_methods == public_methods
    assert forbidden.isdisjoint(public_methods)
    assert "__getattr__" not in GrowwReadClient.__dict__


def test_orders_pages_by_25_until_a_short_page_and_preserves_segment() -> None:
    order_payload = fixture("orders.json")["order_list"][0]

    class PagingSdk:
        def __init__(self) -> None:
            self.calls: list[dict[str, int]] = []

        def get_all_instruments(self) -> pd.DataFrame:
            return pd.DataFrame(fixture("instruments.json"))

        def get_order_list(self, **kwargs: int) -> dict[str, Any]:
            self.calls.append(kwargs)
            count = 25 if kwargs["page"] == 0 else 1
            page = []
            for offset in range(count):
                item = deepcopy(order_payload)
                item["groww_order_id"] = f"INVENTED-{kwargs['page']}-{offset}"
                page.append(item)
            return {"order_list": page}

    sdk = PagingSdk()
    orders = GrowwReadClient(sdk, correlation_id_factory=lambda: "corr-orders").orders()

    assert len(orders) == 26
    assert sdk.calls == [{"page": 0, "page_size": 25}, {"page": 1, "page_size": 25}]
    assert all(order.segment == "FNO" for order in orders)


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [
        ("groww_order_id", 7001),
        ("transaction_type", 7002),
        ("order_status", 7003),
        ("segment", 7004),
    ],
)
def test_orders_rejects_numeric_semantic_strings(field: str, malformed_value: int) -> None:
    class MalformedOrderSdk:
        def get_all_instruments(self) -> pd.DataFrame:
            return pd.DataFrame(fixture("instruments.json"))

        def get_order_list(self, **kwargs: int) -> dict[str, Any]:
            payload = fixture("orders.json")
            payload["order_list"][0][field] = malformed_value
            return payload

    with pytest.raises(BrokerUnavailable) as captured:
        GrowwReadClient(
            MalformedOrderSdk(), correlation_id_factory=lambda: "corr-semantic-string"
        ).orders()

    assert captured.value.safe_view().model_dump() == {
        "code": "GROWW_CONTRACT_MISMATCH",
        "correlation_id": "corr-semantic-string",
        "summary": "Groww returned an unexpected response",
    }


def test_fills_fetches_only_supplied_orders_with_page_size_50_and_deduplicates_ids() -> None:
    trade = fixture("trades.json")["trade_list"][0]

    class FillSdk:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def get_all_instruments(self) -> pd.DataFrame:
            return pd.DataFrame(fixture("instruments.json"))

        def get_trade_list_for_order(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            duplicate_groww_id = deepcopy(trade)
            duplicate_groww_id["exchange_trade_id"] = "OTHER-EXCHANGE-ID"
            duplicate_exchange_id = deepcopy(trade)
            duplicate_exchange_id["groww_trade_id"] = ""
            return {"trade_list": [trade, duplicate_groww_id, duplicate_exchange_id]}

    sdk = FillSdk()
    order = Order(
        order_id="INVENTED-ORDER-0001",
        instrument=instrument(),
        side="BUY",
        quantity=75,
        status="COMPLETE",
        created_at=datetime(2026, 8, 3, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 8, 3, 4, 10, 2, tzinfo=UTC),
        segment="FNO",
        source=MarketDataSource.GROWW_LIVE,
    )

    fills = GrowwReadClient(sdk, correlation_id_factory=lambda: "corr-fills").fills((order,))

    assert [fill.fill_id for fill in fills] == ["INVENTED-TRADE-0001"]
    assert sdk.calls == [
        {
            "groww_order_id": "INVENTED-ORDER-0001",
            "segment": "FNO",
            "page": 0,
            "page_size": 50,
        }
    ]


@pytest.mark.parametrize(
    ("fixture_name", "unsafe_payload_value"),
    [
        ("trades_wrong_order.json", "INVENTED-CROSS-ORDER-SENSITIVE"),
        ("trades_wrong_segment.json", "CASH"),
    ],
)
def test_fills_rejects_trade_from_another_order_or_segment_without_exposing_payload(
    fixture_name: str,
    unsafe_payload_value: str,
) -> None:
    class MismatchedFillSdk:
        def get_all_instruments(self) -> pd.DataFrame:
            return pd.DataFrame(fixture("instruments.json"))

        def get_trade_list_for_order(self, **kwargs: Any) -> dict[str, Any]:
            return fixture(fixture_name)

    order = Order(
        order_id="INVENTED-ORDER-0001",
        instrument=instrument(),
        side="BUY",
        quantity=75,
        status="COMPLETE",
        created_at=datetime(2026, 8, 3, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 8, 3, 4, 10, 2, tzinfo=UTC),
        segment="FNO",
        source=MarketDataSource.GROWW_LIVE,
    )

    with pytest.raises(BrokerUnavailable) as captured:
        GrowwReadClient(
            MismatchedFillSdk(), correlation_id_factory=lambda: "corr-fill-ownership"
        ).fills((order,))

    error = captured.value
    assert error.safe_view().model_dump() == {
        "code": "GROWW_CONTRACT_MISMATCH",
        "correlation_id": "corr-fill-ownership",
        "summary": "Groww returned an unexpected response",
    }
    assert unsafe_payload_value not in str(error)
    assert unsafe_payload_value not in repr(error.safe_view())


def test_fills_rejects_order_without_segment_before_calling_sdk() -> None:
    class NoCallSdk:
        def get_trade_list_for_order(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(f"SDK must not be called: {kwargs}")

    order = Order(
        order_id="INVENTED-ORDER-NO-SEGMENT",
        instrument=instrument(),
        side="BUY",
        quantity=75,
        status="COMPLETE",
        created_at=datetime(2026, 8, 3, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 8, 3, 4, 10, 2, tzinfo=UTC),
    )

    with pytest.raises(BrokerUnavailable) as captured:
        GrowwReadClient(
            NoCallSdk(), correlation_id_factory=lambda: "corr-missing-segment"
        ).fills((order,))

    assert captured.value.safe_view().model_dump() == {
        "code": "GROWW_CONTRACT_MISMATCH",
        "correlation_id": "corr-missing-segment",
        "summary": "Groww returned an unexpected response",
    }


def test_fills_rejects_non_string_segment_before_trade_sdk_call() -> None:
    class RecordingSdk:
        trade_call_reached = False

        def get_all_instruments(self) -> pd.DataFrame:
            return pd.DataFrame(fixture("instruments.json"))

        def get_trade_list_for_order(self, **kwargs: Any) -> dict[str, Any]:
            self.trade_call_reached = True
            return fixture("trades.json")

    malformed_order = Order.model_construct(
        order_id="INVENTED-ORDER-0001",
        instrument=instrument(),
        side="BUY",
        quantity=75,
        status="COMPLETE",
        created_at=datetime(2026, 8, 3, 4, 10, tzinfo=UTC),
        updated_at=datetime(2026, 8, 3, 4, 10, 2, tzinfo=UTC),
        segment=7004,
    )
    sdk = RecordingSdk()

    with pytest.raises(BrokerUnavailable) as captured:
        GrowwReadClient(
            sdk, correlation_id_factory=lambda: "corr-non-string-segment"
        ).fills((malformed_order,))

    assert sdk.trade_call_reached is False
    assert captured.value.safe_view().model_dump() == {
        "code": "GROWW_CONTRACT_MISMATCH",
        "correlation_id": "corr-non-string-segment",
        "summary": "Groww returned an unexpected response",
    }


def test_missing_required_quote_field_is_a_safe_contract_mismatch() -> None:
    class MalformedQuoteSdk:
        def get_all_instruments(self) -> pd.DataFrame:
            return pd.DataFrame(fixture("instruments.json"))

        def get_quote(self, **kwargs: Any) -> dict[str, Any]:
            payload = fixture("quote.json")
            payload.pop("last_price")
            payload["broker_error"] = "sensitive-upstream-detail"
            return payload

    with pytest.raises(BrokerUnavailable) as captured:
        GrowwReadClient(
            MalformedQuoteSdk(), correlation_id_factory=lambda: "corr-malformed-quote"
        ).nifty_quote()

    error = captured.value
    assert error.code == "GROWW_CONTRACT_MISMATCH"
    assert error.correlation_id == "corr-malformed-quote"
    assert "sensitive-upstream-detail" not in str(error)
    assert "sensitive-upstream-detail" not in repr(error.safe_view())
