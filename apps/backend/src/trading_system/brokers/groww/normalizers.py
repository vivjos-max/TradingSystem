"""Strict normalization of documented Groww read responses."""

from __future__ import annotations

from collections.abc import Mapping, MutableSet, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeAlias

import pandas as pd  # type: ignore[import-untyped]

from trading_system.brokers.ports import BrokerUnavailable
from trading_system.domain.models import (
    Fill,
    Instrument,
    MarginSummary,
    MarketDataSource,
    OptionChain,
    OptionContract,
    OptionGreeks,
    Order,
    Position,
    Quote,
)


InstrumentIndex: TypeAlias = Mapping[tuple[str, str], Instrument]


def _mismatch(correlation_id: str) -> BrokerUnavailable:
    return BrokerUnavailable(
        code="GROWW_CONTRACT_MISMATCH",
        correlation_id=correlation_id,
        summary="Groww returned an unexpected response",
    )


def _mapping(value: object, correlation_id: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _mismatch(correlation_id)
    return value


def _sequence(value: object, correlation_id: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _mismatch(correlation_id)
    return value


def _required(payload: Mapping[str, Any], key: str, correlation_id: str) -> Any:
    if key not in payload or _is_missing(payload[key]):
        raise _mismatch(correlation_id)
    return payload[key]


def _optional(payload: Mapping[str, Any], key: str) -> Any | None:
    value = payload.get(key)
    return None if _is_missing(value) else value


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(missing) if isinstance(missing, bool) else False


def _text(value: object, correlation_id: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise _mismatch(correlation_id)
    result = str(value)
    if not result:
        raise _mismatch(correlation_id)
    return result


def _decimal(value: object, correlation_id: str) -> Decimal:
    if isinstance(value, bool):
        raise _mismatch(correlation_id)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise _mismatch(correlation_id) from None
    if not result.is_finite():
        raise _mismatch(correlation_id)
    return result


def _optional_decimal(value: object | None, correlation_id: str) -> Decimal | None:
    return None if _is_missing(value) else _decimal(value, correlation_id)


def _integer(value: object, correlation_id: str) -> int:
    if isinstance(value, bool):
        raise _mismatch(correlation_id)
    numeric = _decimal(value, correlation_id)
    result = int(numeric)
    if numeric != Decimal(result):
        raise _mismatch(correlation_id)
    return result


def _date(value: object | None, correlation_id: str) -> date | None:
    if _is_missing(value):
        return None
    if not isinstance(value, str):
        raise _mismatch(correlation_id)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _mismatch(correlation_id) from None


def _timestamp(value: object, correlation_id: str) -> datetime:
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(float(_decimal(value, correlation_id) / 1000), tz=UTC)
        else:
            raise ValueError
    except (OverflowError, OSError, ValueError):
        raise _mismatch(correlation_id) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _mismatch(correlation_id)
    return parsed.astimezone(UTC)


def normalize_instruments(payload: object, correlation_id: str) -> tuple[Instrument, ...]:
    """Normalize the SDK's documented instrument-master DataFrame."""
    if not isinstance(payload, pd.DataFrame):
        raise _mismatch(correlation_id)
    records = payload.to_dict(orient="records")
    normalized: list[Instrument] = []
    for raw in records:
        row = _mapping(raw, correlation_id)
        instrument_type = _text(_required(row, "instrument_type", correlation_id), correlation_id)
        reserved = _optional(row, "is_reserved")
        buy_allowed = _optional(row, "buy_allowed")
        normalized.append(
            Instrument(
                instrument_token=_text(
                    _required(row, "exchange_token", correlation_id), correlation_id
                ),
                symbol=_text(_required(row, "trading_symbol", correlation_id), correlation_id),
                exchange=_text(_required(row, "exchange", correlation_id), correlation_id),
                instrument_type=instrument_type,
                expiry=_date(_optional(row, "expiry_date"), correlation_id),
                strike=_optional_decimal(_optional(row, "strike_price"), correlation_id),
                option_type=instrument_type if instrument_type in {"CE", "PE"} else None,
                lot_size=_integer(_required(row, "lot_size", correlation_id), correlation_id),
                tick_size=_decimal(_required(row, "tick_size", correlation_id), correlation_id),
                is_reserved=False if reserved is None else bool(_integer(reserved, correlation_id)),
                is_buyable=True if buy_allowed is None else bool(_integer(buy_allowed, correlation_id)),
                source=MarketDataSource.GROWW_LIVE,
            )
        )
    return tuple(normalized)


def index_instruments(instruments: tuple[Instrument, ...]) -> dict[tuple[str, str], Instrument]:
    """Index normalized instruments by the keys present in read responses."""
    return {(instrument.exchange, instrument.symbol): instrument for instrument in instruments}


def _find_instrument(
    index: InstrumentIndex,
    *,
    exchange: object,
    symbol: object,
    correlation_id: str,
) -> Instrument:
    key = (_text(exchange, correlation_id), _text(symbol, correlation_id))
    try:
        return index[key]
    except KeyError:
        raise _mismatch(correlation_id) from None


def normalize_quote(
    payload: object,
    instrument: Instrument,
    correlation_id: str,
) -> Quote:
    """Normalize a documented quote response."""
    quote = _mapping(payload, correlation_id)
    return Quote(
        instrument_token=instrument.instrument_token,
        last_price=_decimal(_required(quote, "last_price", correlation_id), correlation_id),
        observed_at=_timestamp(_required(quote, "last_trade_time", correlation_id), correlation_id),
        source=MarketDataSource.GROWW_LIVE,
        bid_price=_optional_decimal(_optional(quote, "bid_price"), correlation_id),
        ask_price=_optional_decimal(_optional(quote, "offer_price"), correlation_id),
        volume=(
            None
            if _optional(quote, "volume") is None
            else _integer(_optional(quote, "volume"), correlation_id)
        ),
    )


def normalize_option_chain(
    payload: object,
    expiry: date,
    index: InstrumentIndex,
    correlation_id: str,
) -> OptionChain:
    """Normalize option-chain identities while validating the documented response."""
    chain = _mapping(payload, correlation_id)
    _decimal(_required(chain, "underlying_ltp", correlation_id), correlation_id)
    strikes = _mapping(_required(chain, "strikes", correlation_id), correlation_id)
    underlying = _find_instrument(
        index, exchange="NSE", symbol="NIFTY", correlation_id=correlation_id
    )
    contracts: list[OptionContract] = []
    for strike_key in sorted(strikes, key=lambda item: _decimal(item, correlation_id)):
        strike_payload = _mapping(strikes[strike_key], correlation_id)
        for option_type in ("CE", "PE"):
            if option_type not in strike_payload:
                continue
            contract_payload = _mapping(strike_payload[option_type], correlation_id)
            _decimal(_required(contract_payload, "ltp", correlation_id), correlation_id)
            _integer(_required(contract_payload, "open_interest", correlation_id), correlation_id)
            _integer(_required(contract_payload, "volume", correlation_id), correlation_id)
            greeks = _mapping(_required(contract_payload, "greeks", correlation_id), correlation_id)
            for field in ("delta", "gamma", "theta", "vega", "rho", "iv"):
                _optional_decimal(_optional(greeks, field), correlation_id)
            instrument = _find_instrument(
                index,
                exchange="NSE",
                symbol=_required(contract_payload, "trading_symbol", correlation_id),
                correlation_id=correlation_id,
            )
            if instrument.expiry != expiry or instrument.option_type != option_type:
                raise _mismatch(correlation_id)
            contracts.append(OptionContract(instrument=instrument))
    return OptionChain(
        underlying=underlying,
        expiry=expiry,
        contracts=tuple(contracts),
        source=MarketDataSource.GROWW_LIVE,
    )


def normalize_greeks(
    payload: object,
    contract: OptionContract,
    correlation_id: str,
) -> OptionGreeks:
    """Normalize the documented Greeks envelope for one contract."""
    envelope = _mapping(payload, correlation_id)
    greeks = _mapping(_required(envelope, "greeks", correlation_id), correlation_id)
    return OptionGreeks(
        instrument=contract.instrument,
        observed_at=None,
        source=MarketDataSource.GROWW_LIVE,
        delta=_optional_decimal(_optional(greeks, "delta"), correlation_id),
        gamma=_optional_decimal(_optional(greeks, "gamma"), correlation_id),
        theta=_optional_decimal(_optional(greeks, "theta"), correlation_id),
        vega=_optional_decimal(_optional(greeks, "vega"), correlation_id),
        rho=_optional_decimal(_optional(greeks, "rho"), correlation_id),
        implied_volatility=_optional_decimal(_optional(greeks, "iv"), correlation_id),
    )


def normalize_margin(payload: object, correlation_id: str) -> MarginSummary:
    """Normalize available margin values from the documented margin response."""
    margin = _mapping(payload, correlation_id)
    return MarginSummary(
        available_cash=_decimal(_required(margin, "clear_cash", correlation_id), correlation_id),
        used_margin=_decimal(
            _required(margin, "net_margin_used", correlation_id), correlation_id
        ),
        collateral=_decimal(
            _required(margin, "collateral_available", correlation_id), correlation_id
        ),
        observed_at=None,
        source=MarketDataSource.GROWW_LIVE,
    )


def normalize_positions(
    payload: object,
    index: InstrumentIndex,
    correlation_id: str,
) -> tuple[Position, ...]:
    """Normalize the documented position list."""
    envelope = _mapping(payload, correlation_id)
    rows = _sequence(_required(envelope, "positions", correlation_id), correlation_id)
    positions: list[Position] = []
    for raw in rows:
        row = _mapping(raw, correlation_id)
        instrument = _find_instrument(
            index,
            exchange=_required(row, "exchange", correlation_id),
            symbol=_required(row, "trading_symbol", correlation_id),
            correlation_id=correlation_id,
        )
        realized = _optional(row, "realised_pnl")
        positions.append(
            Position(
                instrument=instrument,
                quantity=_integer(_required(row, "quantity", correlation_id), correlation_id),
                average_price=_decimal(
                    _required(row, "net_price", correlation_id), correlation_id
                ),
                last_price=None,
                realized_pnl=(
                    Decimal("0") if realized is None else _decimal(realized, correlation_id)
                ),
                unrealized_pnl=Decimal("0"),
                observed_at=None,
                source=MarketDataSource.GROWW_LIVE,
            )
        )
    return tuple(positions)


def normalize_orders(
    payload: object,
    index: InstrumentIndex,
    correlation_id: str,
) -> tuple[Order, ...]:
    """Normalize one documented order-list page."""
    envelope = _mapping(payload, correlation_id)
    rows = _sequence(_required(envelope, "order_list", correlation_id), correlation_id)
    orders: list[Order] = []
    for raw in rows:
        row = _mapping(raw, correlation_id)
        instrument = _find_instrument(
            index,
            exchange=_required(row, "exchange", correlation_id),
            symbol=_required(row, "trading_symbol", correlation_id),
            correlation_id=correlation_id,
        )
        orders.append(
            Order(
                order_id=_text(_required(row, "groww_order_id", correlation_id), correlation_id),
                instrument=instrument,
                side=_text(_required(row, "transaction_type", correlation_id), correlation_id),
                quantity=_integer(_required(row, "quantity", correlation_id), correlation_id),
                status=_text(_required(row, "order_status", correlation_id), correlation_id),
                created_at=_timestamp(
                    _required(row, "created_at", correlation_id), correlation_id
                ),
                updated_at=_timestamp(
                    _required(row, "exchange_time", correlation_id), correlation_id
                ),
                limit_price=_optional_decimal(_optional(row, "price"), correlation_id),
                segment=_text(_required(row, "segment", correlation_id), correlation_id),
                source=MarketDataSource.GROWW_LIVE,
            )
        )
    return tuple(orders)


def normalize_fills(
    payload: object,
    index: InstrumentIndex,
    correlation_id: str,
    seen_groww_ids: MutableSet[str],
    seen_exchange_ids: MutableSet[str],
) -> tuple[Fill, ...]:
    """Normalize one trade list and deduplicate both documented identifiers."""
    envelope = _mapping(payload, correlation_id)
    rows = _sequence(_required(envelope, "trade_list", correlation_id), correlation_id)
    fills: list[Fill] = []
    for raw in rows:
        row = _mapping(raw, correlation_id)
        groww_id_value = _optional(row, "groww_trade_id")
        exchange_id_value = _optional(row, "exchange_trade_id")
        groww_id = None if groww_id_value in (None, "") else _text(groww_id_value, correlation_id)
        exchange_id = (
            None
            if exchange_id_value in (None, "")
            else _text(exchange_id_value, correlation_id)
        )
        if groww_id is None and exchange_id is None:
            raise _mismatch(correlation_id)
        duplicate = (groww_id is not None and groww_id in seen_groww_ids) or (
            exchange_id is not None and exchange_id in seen_exchange_ids
        )
        if groww_id is not None:
            seen_groww_ids.add(groww_id)
        if exchange_id is not None:
            seen_exchange_ids.add(exchange_id)
        if duplicate:
            continue
        instrument = _find_instrument(
            index,
            exchange=_required(row, "exchange", correlation_id),
            symbol=_required(row, "trading_symbol", correlation_id),
            correlation_id=correlation_id,
        )
        fills.append(
            Fill(
                fill_id=groww_id or exchange_id or "",
                order_id=_text(
                    _required(row, "groww_order_id", correlation_id), correlation_id
                ),
                instrument=instrument,
                quantity=_integer(_required(row, "quantity", correlation_id), correlation_id),
                price=_decimal(_required(row, "price", correlation_id), correlation_id),
                filled_at=_timestamp(
                    _required(row, "trade_date_time", correlation_id), correlation_id
                ),
                source=MarketDataSource.GROWW_LIVE,
            )
        )
    return tuple(fills)
