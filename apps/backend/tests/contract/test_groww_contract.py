from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_system.brokers.groww.client import GrowwReadClient
from trading_system.brokers.ports import BrokerUnavailable
from trading_system.domain.models import MarketDataSource


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    with (FIXTURES / name).open(encoding="utf-8") as source:
        return json.load(source, parse_float=Decimal)


class DocumentedGrowwSdk:
    """Test boundary containing only official read calls and fixture responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_all_instruments(self) -> pd.DataFrame:
        self.calls.append(("get_all_instruments", {}))
        return pd.DataFrame(fixture("instruments.json"))

    def get_quote(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_quote", kwargs))
        return fixture("quote.json")

    def get_option_chain(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_option_chain", kwargs))
        return fixture("option_chain.json")

    def get_greeks(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_greeks", kwargs))
        return fixture("greeks.json")

    def get_available_margin_details(self) -> dict[str, Any]:
        self.calls.append(("get_available_margin_details", {}))
        return fixture("margin.json")

    def get_positions_for_user(self) -> dict[str, Any]:
        self.calls.append(("get_positions_for_user", {}))
        return fixture("positions.json")

    def get_order_list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_order_list", kwargs))
        return fixture("orders.json")

    def get_trade_list_for_order(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_trade_list_for_order", kwargs))
        return fixture("trades.json")


def test_documented_contract_shapes_normalize_to_immutable_models() -> None:
    sdk = DocumentedGrowwSdk()
    client = GrowwReadClient(sdk, correlation_id_factory=lambda: "corr-contract-1")

    instruments = client.instruments()
    quote = client.nifty_quote()
    chain = client.option_chain(date(2026, 8, 6))
    greeks = client.greeks((chain.contracts[0],))
    margin = client.margin()
    positions = client.positions()
    orders = client.orders()
    fills = client.fills(orders)

    assert [item.instrument_token for item in instruments] == ["26000", "88001", "88002"]
    assert instruments[0].expiry is None
    assert instruments[0].strike is None
    assert quote.model_dump() == {
        "instrument_token": "26000",
        "last_price": Decimal("24798.25"),
        "observed_at": datetime(2026, 8, 3, 4, 15, tzinfo=UTC),
        "source": MarketDataSource.GROWW_LIVE,
        "bid_price": Decimal("24798.1"),
        "ask_price": Decimal("24798.35"),
        "volume": 987654,
    }
    assert chain.expiry == date(2026, 8, 6)
    assert chain.underlying.instrument_token == "26000"
    assert [contract.instrument_token for contract in chain.contracts] == ["88001", "88002"]
    assert all(contract.source is MarketDataSource.GROWW_LIVE for contract in chain.contracts)
    assert greeks[0].instrument == chain.contracts[0].instrument
    assert greeks[0].model_dump(exclude={"instrument"}) == {
        "observed_at": None,
        "source": MarketDataSource.GROWW_LIVE,
        "delta": Decimal("0.5123"),
        "gamma": Decimal("0.0012"),
        "theta": Decimal("-11.4567"),
        "vega": Decimal("18.7654"),
        "rho": Decimal("2.3456"),
        "implied_volatility": Decimal("12.3456"),
    }
    assert margin.model_dump() == {
        "available_cash": Decimal("123456.78"),
        "used_margin": Decimal("23456.7"),
        "collateral": Decimal("5432.1"),
        "observed_at": None,
        "source": MarketDataSource.GROWW_LIVE,
    }
    assert positions[0].instrument == instruments[1]
    assert positions[0].model_dump(exclude={"instrument"}) == {
        "quantity": 75,
        "average_price": Decimal("136.75"),
        "last_price": None,
        "realized_pnl": Decimal("618.75"),
        "unrealized_pnl": None,
        "observed_at": None,
        "source": MarketDataSource.GROWW_LIVE,
    }
    assert orders[0].order_id == "INVENTED-ORDER-0001"
    assert orders[0].segment == "FNO"
    assert orders[0].created_at == datetime(2026, 8, 3, 4, 10, tzinfo=UTC)
    assert orders[0].updated_at == datetime(2026, 8, 3, 4, 10, 2, tzinfo=UTC)
    assert fills[0].fill_id == "INVENTED-TRADE-0001"
    assert fills[0].filled_at == datetime(2026, 8, 3, 4, 10, 2, tzinfo=UTC)

    top_level = (*instruments, quote, chain, *greeks, margin, *positions, *orders, *fills)
    assert all(item.source is MarketDataSource.GROWW_LIVE for item in top_level)
    nested_instruments = (
        chain.underlying,
        *(contract.instrument for contract in chain.contracts),
        *(item.instrument for item in greeks),
        *(item.instrument for item in positions),
        *(item.instrument for item in orders),
        *(item.instrument for item in fills),
    )
    assert all(item.source is MarketDataSource.GROWW_LIVE for item in nested_instruments)
    assert sdk.calls == [
        ("get_all_instruments", {}),
        (
            "get_quote",
            {"trading_symbol": "NIFTY", "exchange": "NSE", "segment": "CASH"},
        ),
        (
            "get_option_chain",
            {"exchange": "NSE", "underlying": "NIFTY", "expiry_date": "2026-08-06"},
        ),
        (
            "get_greeks",
            {
                "exchange": "NSE",
                "underlying": "NIFTY",
                "trading_symbol": "NIFTY26AUG24800CE",
                "expiry": "2026-08-06",
            },
        ),
        ("get_available_margin_details", {}),
        ("get_positions_for_user", {}),
        ("get_order_list", {"page": 0, "page_size": 25}),
        (
            "get_trade_list_for_order",
            {
                "groww_order_id": "INVENTED-ORDER-0001",
                "segment": "FNO",
                "page": 0,
                "page_size": 50,
            },
        ),
    ]


def test_documented_optional_quote_values_normalize_to_none() -> None:
    class OptionalQuoteSdk(DocumentedGrowwSdk):
        def get_quote(self, **kwargs: Any) -> dict[str, Any]:
            payload = deepcopy(fixture("quote.json"))
            payload.pop("bid_price")
            payload.pop("offer_price")
            payload.pop("volume")
            return payload

    quote = GrowwReadClient(
        OptionalQuoteSdk(), correlation_id_factory=lambda: "corr-contract-2"
    ).nifty_quote()

    assert quote.bid_price is None
    assert quote.ask_price is None
    assert quote.volume is None


def test_documented_optional_order_price_normalizes_to_none() -> None:
    class OptionalOrderSdk(DocumentedGrowwSdk):
        def get_order_list(self, **kwargs: Any) -> dict[str, Any]:
            payload = deepcopy(fixture("orders.json"))
            payload["order_list"][0].pop("price")
            return payload

    orders = GrowwReadClient(
        OptionalOrderSdk(), correlation_id_factory=lambda: "corr-contract-3"
    ).orders()

    assert orders[0].limit_price is None


def test_missing_documented_realized_pnl_is_a_contract_mismatch_not_zero() -> None:
    class MissingRealizedPnlSdk(DocumentedGrowwSdk):
        def get_positions_for_user(self) -> dict[str, Any]:
            payload = deepcopy(fixture("positions.json"))
            payload["positions"][0].pop("realised_pnl")
            return payload

    with pytest.raises(BrokerUnavailable) as captured:
        GrowwReadClient(
            MissingRealizedPnlSdk(), correlation_id_factory=lambda: "corr-position-pnl"
        ).positions()

    assert captured.value.safe_view().model_dump() == {
        "code": "GROWW_CONTRACT_MISMATCH",
        "correlation_id": "corr-position-pnl",
        "summary": "Groww returned an unexpected response",
    }
