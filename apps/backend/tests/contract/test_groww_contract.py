import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.groww import GrowwError, GrowwReadClient
from trading_system.market import resolve_nifty_universe

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, *, orient):
        assert orient == "records"
        return self.rows


class DocumentedSDK:
    def get_all_instruments(self):
        return Frame(fixture("instruments.json"))

    def get_quote(self, **kwargs):
        return fixture("quote.json")

    def get_option_chain(self, **kwargs):
        return fixture("option_chain.json")

    def get_available_margin_details(self, *, timeout):
        assert timeout == 5
        return fixture("margin.json")

    def get_positions_for_user(self, *, timeout):
        assert timeout == 5
        return fixture("positions.json")

    def get_order_list(self, *, page, page_size, segment, timeout):
        assert page_size == 25 and timeout == 5
        return fixture("orders.json") if segment == "FNO" and page == 0 else {"order_list": []}

    def get_trade_list_for_order(self, *, groww_order_id, segment, page, page_size, timeout):
        assert (groww_order_id, segment, page_size, timeout) == ("ORDER-1", "FNO", 50, 5)
        return fixture("trades.json") if page == 0 else {"trade_list": []}


def test_documented_shapes_normalize_without_market_depth_synthesis():
    client = GrowwReadClient(DocumentedSDK())
    instruments = client.instruments()
    universe = resolve_nifty_universe(instruments, Decimal("22510.5"), date(2026, 8, 10))
    observed_at = datetime(2026, 8, 10, 10, tzinfo=timezone.utc)

    market, options = client.market_snapshot(universe, observed_at)
    account = client.account_snapshot(observed_at)
    orders = client.orders_and_fills()

    assert market.spot.price == Decimal("22510.5")
    assert options.contracts[0].ltp == Decimal("120.5")
    assert options.contracts[0].bid is None and options.contracts[0].ask is None
    assert options.contracts[1].ltp is None and options.contracts[1].volume is None
    assert account.available_margin == Decimal("96.21")
    assert account.positions[0].average_price == Decimal("22400")
    assert orders[0].fills[0].trade_id == "TRADE-1"
    assert client.audit_raw["market_snapshot"]["spot_quote"] == fixture("quote.json")
    assert client.audit_raw["account_snapshot"]["margin"] == fixture("margin.json")


def test_missing_required_documented_field_is_contract_mismatch():
    class MalformedSDK(DocumentedSDK):
        def get_quote(self, **kwargs):
            return {"last_trade_time": 1786340700000}

    client = GrowwReadClient(MalformedSDK())
    instruments = GrowwReadClient(DocumentedSDK()).instruments()
    universe = resolve_nifty_universe(instruments, Decimal("22510.5"), date(2026, 8, 10))

    with pytest.raises(GrowwError) as raised:
        client.market_snapshot(universe)

    assert raised.value.detail.code == "GROWW_CONTRACT_MISMATCH"


def test_index_quote_can_use_receipt_time_but_tradable_quote_time_is_required():
    class MissingIndexTime(DocumentedSDK):
        def get_quote(self, **kwargs):
            return {"last_price": 22510.5} if kwargs["segment"] == "CASH" else fixture("quote.json")

    class MissingFutureTime(DocumentedSDK):
        def get_quote(self, **kwargs):
            return {"last_price": 22510.5} if kwargs["segment"] == "FNO" else fixture("quote.json")

    instruments = GrowwReadClient(DocumentedSDK()).instruments()
    universe = resolve_nifty_universe(instruments, Decimal("22510.5"), date(2026, 8, 10))
    observed_at = datetime(2026, 8, 10, 10, tzinfo=timezone.utc)
    market, _ = GrowwReadClient(MissingIndexTime()).market_snapshot(universe, observed_at)

    assert market.spot.source_at == observed_at
    with pytest.raises(GrowwError) as raised:
        GrowwReadClient(MissingFutureTime()).market_snapshot(universe, observed_at)
    assert raised.value.detail.code == "GROWW_CONTRACT_MISMATCH"


def test_missing_required_option_contract_fails_closed():

    class PartialChain(DocumentedSDK):
        def get_option_chain(self, **kwargs):
            payload = fixture("option_chain.json")
            payload["strikes"]["22500"].pop("PE")
            return payload

    instruments = GrowwReadClient(DocumentedSDK()).instruments()
    universe = resolve_nifty_universe(instruments, Decimal("22510.5"), date(2026, 8, 10))
    with pytest.raises(GrowwError) as raised:
        GrowwReadClient(PartialChain()).market_snapshot(universe)
    assert raised.value.detail.code == "GROWW_CONTRACT_MISMATCH"
