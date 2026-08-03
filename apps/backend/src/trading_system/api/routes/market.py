"""Read-only normalized NIFTY quote, option-chain, and candle views."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, ValidationError

from trading_system.api.dependencies import AppContainer
from trading_system.api.routes.health import (
    SafeUnavailableResponse,
    correlation_id,
    get_ready_container,
    unavailable_response,
    utc_now,
)


class _QuotePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    last_price: Decimal
    source: str


class QuoteSnapshotResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_key: str
    last_price: Decimal
    source: str
    source_observed_at: datetime
    served_at: datetime
    correlation_id: str


class NiftyMarketResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    spot: QuoteSnapshotResponse
    future: QuoteSnapshotResponse
    served_at: datetime
    correlation_id: str


class _OptionContractPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    instrument_token: str
    strike: Decimal
    option_type: str
    last_price: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    spread: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None


class _OptionChainPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    expiry: str
    source: str
    atm_strike: Decimal
    selected_strikes: list[Decimal]
    contracts: list[_OptionContractPayload]


class OptionContractResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_token: str
    strike: Decimal
    option_type: str
    last_price: Decimal | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    spread: Decimal | None
    volume: int | None
    open_interest: int | None
    implied_volatility: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    rho: Decimal | None


class NiftyOptionChainResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    expiry: str
    source: str
    atm_strike: Decimal
    source_observed_at: datetime
    contracts: list[OptionContractResponse]
    served_at: datetime
    correlation_id: str


class CandleResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_key: str
    interval: str
    source_opened_at: datetime
    source_closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None
    finalized: bool
    source: str


class NiftyCandlesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    interval: str
    candles: list[CandleResponse]
    served_at: datetime
    correlation_id: str


router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get(
    "/nifty",
    response_model=NiftyMarketResponse,
    responses={503: {"model": SafeUnavailableResponse}},
)
async def nifty_quote(
    container: AppContainer = Depends(get_ready_container),
) -> NiftyMarketResponse | object:
    try:
        if not container.nifty_future_instrument_key:
            return unavailable_response()
        spot = await container.live_state.get_quote(container.nifty_instrument_key)
        future = await container.live_state.get_quote(container.nifty_future_instrument_key)
        if spot is None or future is None:
            return unavailable_response()
        spot_payload = _QuotePayload.model_validate(spot.payload)
        future_payload = _QuotePayload.model_validate(future.payload)
    except (TypeError, ValueError, ValidationError):
        return unavailable_response()
    except Exception:
        return unavailable_response()
    served_at = utc_now()
    return NiftyMarketResponse(
        spot=QuoteSnapshotResponse(
            instrument_key=spot.instrument_key, last_price=spot_payload.last_price,
            source=spot_payload.source, source_observed_at=spot.observed_at,
            served_at=served_at, correlation_id=correlation_id(),
        ),
        future=QuoteSnapshotResponse(
            instrument_key=future.instrument_key, last_price=future_payload.last_price,
            source=future_payload.source, source_observed_at=future.observed_at,
            served_at=served_at, correlation_id=correlation_id(),
        ),
        served_at=served_at,
        correlation_id=correlation_id(),
    )


@router.get(
    "/nifty/options",
    response_model=NiftyOptionChainResponse,
    responses={503: {"model": SafeUnavailableResponse}},
)
async def nifty_options(
    container: AppContainer = Depends(get_ready_container),
) -> NiftyOptionChainResponse | object:
    try:
        chain = await container.live_state.get_option_chain(container.option_chain_key)
        if chain is None:
            return unavailable_response()
        payload = _OptionChainPayload.model_validate(chain.payload)
    except (TypeError, ValueError, ValidationError):
        return unavailable_response()
    except Exception:
        return unavailable_response()
    allowed_count = 2 * container.option_strikes_each_side + 1
    selected = payload.selected_strikes
    if (
        container.option_strikes_each_side < 0
        or len(selected) > allowed_count
        or payload.atm_strike not in selected
        or any(contract.strike not in selected for contract in payload.contracts)
    ):
        return unavailable_response()
    return NiftyOptionChainResponse(
        expiry=payload.expiry,
        source=payload.source,
        atm_strike=payload.atm_strike,
        source_observed_at=chain.observed_at,
        contracts=[
            OptionContractResponse(
                instrument_token=contract.instrument_token,
                strike=contract.strike,
                option_type=contract.option_type,
                last_price=contract.last_price,
                bid_price=contract.bid_price, ask_price=contract.ask_price,
                spread=contract.spread, volume=contract.volume,
                open_interest=contract.open_interest,
                implied_volatility=contract.implied_volatility, delta=contract.delta,
                gamma=contract.gamma, theta=contract.theta, vega=contract.vega, rho=contract.rho,
            )
            for contract in payload.contracts
        ],
        served_at=utc_now(),
        correlation_id=correlation_id(),
    )


@router.get(
    "/nifty/candles",
    response_model=NiftyCandlesResponse,
    responses={503: {"model": SafeUnavailableResponse}},
)
async def nifty_candles(
    interval: Literal["1m", "5m"] = Query(...),
    container: AppContainer = Depends(get_ready_container),
) -> NiftyCandlesResponse | object:
    try:
        records = await container.repository.recent_candles(
            container.nifty_instrument_key, interval, limit=container.candle_history_limit
        )
    except Exception:
        return unavailable_response()
    candles = [
        CandleResponse(
            instrument_key=record.instrument_key,
            interval=record.interval,
            source_opened_at=record.opened_at,
            source_closed_at=record.closed_at,
            open=record.open,
            high=record.high,
            low=record.low,
            close=record.close,
            volume=record.volume,
            finalized=record.finalized,
            source=record.source,
        )
        for record in records
        if record.instrument_key == container.nifty_instrument_key and record.interval == interval
    ]
    if not candles:
        return unavailable_response()
    return NiftyCandlesResponse(
        interval=interval,
        candles=candles,
        served_at=utc_now(),
        correlation_id=correlation_id(),
    )


__all__ = ["router"]
