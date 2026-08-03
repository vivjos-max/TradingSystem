"""Read-only normalized account summary and order views."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from trading_system.api.dependencies import AppContainer
from trading_system.api.routes.health import (
    SafeUnavailableResponse,
    correlation_id,
    get_ready_container,
    unavailable_response,
    utc_now,
)


class _AccountPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    available_cash: Decimal
    used_margin: Decimal
    collateral: Decimal = Decimal("0")
    source: str


class AccountSummaryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available_cash: Decimal
    used_margin: Decimal
    collateral: Decimal
    source: str
    source_observed_at: datetime
    served_at: datetime
    correlation_id: str


class _OrderPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    order_id: str
    instrument_token: str
    side: str
    quantity: int
    status: str
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def source_timestamps_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source timestamp must be timezone-aware")
        return value.astimezone(UTC)


class _OrdersPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    orders: list[_OrderPayload]


class OrderResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    instrument_token: str
    side: str
    quantity: int
    status: str
    source_created_at: datetime
    source_updated_at: datetime
    source: str
    source_observed_at: datetime


class AccountOrdersResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    orders: list[OrderResponse]
    served_at: datetime
    correlation_id: str


router = APIRouter(prefix="/api/v1/account", tags=["account"])


@router.get(
    "/summary",
    response_model=AccountSummaryResponse,
    responses={503: {"model": SafeUnavailableResponse}},
)
async def account_summary(
    container: AppContainer = Depends(get_ready_container),
) -> AccountSummaryResponse | object:
    try:
        summary = await container.live_state.get_account_summary(container.account_key)
        if summary is None:
            return unavailable_response()
        payload = _AccountPayload.model_validate(summary.payload)
    except (TypeError, ValueError, ValidationError):
        return unavailable_response()
    except Exception:
        return unavailable_response()
    return AccountSummaryResponse(
        available_cash=payload.available_cash,
        used_margin=payload.used_margin,
        collateral=payload.collateral,
        source=payload.source,
        source_observed_at=summary.observed_at,
        served_at=utc_now(),
        correlation_id=correlation_id(),
    )


@router.get(
    "/orders",
    response_model=AccountOrdersResponse,
    responses={503: {"model": SafeUnavailableResponse}},
)
async def account_orders(
    container: AppContainer = Depends(get_ready_container),
) -> AccountOrdersResponse | object:
    try:
        snapshots = await container.repository.latest_broker_snapshots("orders")
        account_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot.snapshot_key == container.account_key
        ]
        if not account_snapshots:
            return unavailable_response()
        snapshot = max(account_snapshots, key=lambda item: item.observed_at)
        payload_source = snapshot.raw_payload.get("normalized", snapshot.raw_payload)
        payload = _OrdersPayload.model_validate(payload_source)
    except (AttributeError, TypeError, ValueError, ValidationError):
        return unavailable_response()
    except Exception:
        return unavailable_response()
    return AccountOrdersResponse(
        orders=[
            OrderResponse(
                order_id=order.order_id,
                instrument_token=order.instrument_token,
                side=order.side,
                quantity=order.quantity,
                status=order.status,
                source_created_at=order.created_at,
                source_updated_at=order.updated_at,
                source=snapshot.source,
                source_observed_at=snapshot.observed_at,
            )
            for order in payload.orders
        ],
        served_at=utc_now(),
        correlation_id=correlation_id(),
    )


__all__ = ["router"]
