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


class NiftyQuoteResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_key: str
    last_price: Decimal
    source: str
    source_observed_at: datetime
    served_at: datetime
    correlation_id: str


class _OptionContractPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    instrument_token: str
    strike: Decimal
    option_type: str
    last_price: Decimal | None = None


class _OptionChainPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    expiry: str
    source: str
    contracts: list[_OptionContractPayload]


class OptionContractResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_token: str
    strike: Decimal
    option_type: str
    last_price: Decimal | None


class NiftyOptionChainResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    expiry: str
    source: str
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
    response_model=NiftyQuoteResponse,
    responses={503: {"model": SafeUnavailableResponse}},
)
async def nifty_quote(
    container: AppContainer = Depends(get_ready_container),
) -> NiftyQuoteResponse | object:
    try:
        quote = await container.live_state.get_quote(container.nifty_instrument_key)
        if quote is None:
            return unavailable_response()
        payload = _QuotePayload.model_validate(quote.payload)
    except (TypeError, ValueError, ValidationError):
        return unavailable_response()
    except Exception:
        return unavailable_response()
    return NiftyQuoteResponse(
        instrument_key=quote.instrument_key,
        last_price=payload.last_price,
        source=payload.source,
        source_observed_at=quote.observed_at,
        served_at=utc_now(),
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
    return NiftyOptionChainResponse(
        expiry=payload.expiry,
        source=payload.source,
        source_observed_at=chain.observed_at,
        contracts=[
            OptionContractResponse(
                instrument_token=contract.instrument_token,
                strike=contract.strike,
                option_type=contract.option_type,
                last_price=contract.last_price,
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
        records = await container.repository.latest_candles(finalized=True)
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
