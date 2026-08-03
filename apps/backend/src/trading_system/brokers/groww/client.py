"""Explicit read-only Groww SDK adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Protocol
from uuid import uuid4

from trading_system.brokers.groww.auth import map_groww_failure
from trading_system.brokers.groww.normalizers import (
    index_instruments,
    normalize_fills,
    normalize_greeks,
    normalize_instruments,
    normalize_margin,
    normalize_option_chain,
    normalize_orders,
    normalize_positions,
    normalize_quote,
    validate_groww_segment,
)
from trading_system.brokers.ports import BrokerReadPort, BrokerUnavailable
from trading_system.domain.models import (
    Fill,
    Instrument,
    MarginSummary,
    OptionChain,
    OptionContract,
    OptionGreeks,
    Order,
    Position,
    Quote,
)


class _GrowwReadSdk(Protocol):
    def get_all_instruments(self) -> Any: ...

    def get_quote(self, *, trading_symbol: str, exchange: str, segment: str) -> dict[str, Any]: ...

    def get_option_chain(
        self, *, exchange: str, underlying: str, expiry_date: str
    ) -> dict[str, Any]: ...

    def get_greeks(
        self, *, exchange: str, underlying: str, trading_symbol: str, expiry: str
    ) -> dict[str, Any]: ...

    def get_available_margin_details(self) -> dict[str, Any]: ...

    def get_positions_for_user(self) -> dict[str, Any]: ...

    def get_order_list(self, *, page: int, page_size: int) -> dict[str, Any]: ...

    def get_trade_list_for_order(
        self, *, groww_order_id: str, segment: str, page: int, page_size: int
    ) -> dict[str, Any]: ...


class GrowwReadClient(BrokerReadPort):
    """Expose exactly the approved Groww read operations."""

    def __init__(
        self,
        sdk_client: _GrowwReadSdk,
        *,
        correlation_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self._sdk_client = sdk_client
        self._correlation_id_factory = correlation_id_factory
        self._instrument_cache: tuple[Instrument, ...] | None = None

    def instruments(self) -> tuple[Instrument, ...]:
        correlation_id = self._correlation_id_factory()
        return self._load_instruments(correlation_id, refresh=True)

    def nifty_quote(self) -> Quote:
        correlation_id = self._correlation_id_factory()
        index = index_instruments(self._load_instruments(correlation_id))
        try:
            instrument = index[("NSE", "NIFTY")]
        except KeyError:
            raise self._contract_mismatch(correlation_id) from None
        payload = self._invoke(
            correlation_id,
            self._sdk_client.get_quote,
            trading_symbol="NIFTY",
            exchange="NSE",
            segment="CASH",
        )
        return normalize_quote(payload, instrument, correlation_id)

    def option_chain(self, expiry: date) -> OptionChain:
        correlation_id = self._correlation_id_factory()
        index = index_instruments(self._load_instruments(correlation_id))
        payload = self._invoke(
            correlation_id,
            self._sdk_client.get_option_chain,
            exchange="NSE",
            underlying="NIFTY",
            expiry_date=expiry.isoformat(),
        )
        return normalize_option_chain(payload, expiry, index, correlation_id)

    def greeks(self, contracts: tuple[OptionContract, ...]) -> tuple[OptionGreeks, ...]:
        correlation_id = self._correlation_id_factory()
        normalized: list[OptionGreeks] = []
        for contract in contracts:
            expiry = contract.instrument.expiry
            if expiry is None:
                raise self._contract_mismatch(correlation_id)
            payload = self._invoke(
                correlation_id,
                self._sdk_client.get_greeks,
                exchange=contract.instrument.exchange,
                underlying="NIFTY",
                trading_symbol=contract.instrument.symbol,
                expiry=expiry.isoformat(),
            )
            normalized.append(normalize_greeks(payload, contract, correlation_id))
        return tuple(normalized)

    def margin(self) -> MarginSummary:
        correlation_id = self._correlation_id_factory()
        payload = self._invoke(correlation_id, self._sdk_client.get_available_margin_details)
        return normalize_margin(payload, correlation_id)

    def positions(self) -> tuple[Position, ...]:
        correlation_id = self._correlation_id_factory()
        index = index_instruments(self._load_instruments(correlation_id))
        payload = self._invoke(correlation_id, self._sdk_client.get_positions_for_user)
        return normalize_positions(payload, index, correlation_id)

    def orders(self) -> tuple[Order, ...]:
        correlation_id = self._correlation_id_factory()
        index = index_instruments(self._load_instruments(correlation_id))
        orders: list[Order] = []
        page = 0
        while True:
            payload = self._invoke(
                correlation_id,
                self._sdk_client.get_order_list,
                page=page,
                page_size=25,
            )
            current = normalize_orders(payload, index, correlation_id)
            orders.extend(current)
            if len(current) < 25:
                return tuple(orders)
            page += 1

    def fills(self, orders: tuple[Order, ...]) -> tuple[Fill, ...]:
        correlation_id = self._correlation_id_factory()
        validated_orders: list[tuple[Order, str]] = []
        for order in orders:
            if not isinstance(order.order_id, str) or not order.order_id.strip():
                raise self._contract_mismatch(correlation_id)
            segment = validate_groww_segment(order.segment, correlation_id)
            validated_orders.append((order, segment))
        index = index_instruments(self._load_instruments(correlation_id))
        seen_groww_ids: set[str] = set()
        seen_exchange_ids: set[str] = set()
        fills: list[Fill] = []
        for order, segment in validated_orders:
            payload = self._invoke(
                correlation_id,
                self._sdk_client.get_trade_list_for_order,
                groww_order_id=order.order_id,
                segment=segment,
                page=0,
                page_size=50,
            )
            fills.extend(
                normalize_fills(
                    payload,
                    index,
                    correlation_id,
                    order.order_id,
                    segment,
                    seen_groww_ids,
                    seen_exchange_ids,
                )
            )
        return tuple(fills)

    def _load_instruments(
        self,
        correlation_id: str,
        *,
        refresh: bool = False,
    ) -> tuple[Instrument, ...]:
        if refresh or self._instrument_cache is None:
            payload = self._invoke(correlation_id, self._sdk_client.get_all_instruments)
            self._instrument_cache = normalize_instruments(payload, correlation_id)
        return self._instrument_cache

    @staticmethod
    def _contract_mismatch(correlation_id: str) -> BrokerUnavailable:
        return BrokerUnavailable(
            code="GROWW_CONTRACT_MISMATCH",
            correlation_id=correlation_id,
            summary="Groww returned an unexpected response",
        )

    @staticmethod
    def _invoke(
        correlation_id: str,
        operation: Callable[..., Any],
        **kwargs: Any,
    ) -> Any:
        try:
            return operation(**kwargs)
        except BrokerUnavailable:
            raise
        except Exception as error:
            raise map_groww_failure(error, correlation_id) from None
