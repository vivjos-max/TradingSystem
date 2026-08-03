"""Read-only normalized account summary and order views."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

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
    positions: list["_PositionPayload"] = []


class _PositionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    instrument_token: str
    quantity: int
    average_price: Decimal
    last_price: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal | None = None
    observed_at: datetime | None = None
    source: str | None = None


class AccountSummaryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    available_cash: Decimal
    used_margin: Decimal
    collateral: Decimal
    source: str
    source_observed_at: datetime
    positions: list["PositionResponse"]
    served_at: datetime
    correlation_id: str


class PositionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_token: str
    quantity: int
    average_price: Decimal
    last_price: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    source: str
    source_observed_at: datetime


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


class _FillPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    fill_id: str
    order_id: str
    instrument_token: str
    quantity: int
    price: Decimal
    filled_at: datetime

    @field_validator("filled_at", mode="after")
    @classmethod
    def source_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source timestamp must be timezone-aware")
        return value.astimezone(UTC)


class _FillsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    fills: list[_FillPayload]


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
    fills: list["FillResponse"]


class FillResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: str
    order_id: str
    instrument_token: str
    quantity: int
    price: Decimal
    source_filled_at: datetime
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
        positions=[
            PositionResponse(
                instrument_token=position.instrument_token,
                quantity=position.quantity,
                average_price=position.average_price,
                last_price=position.last_price,
                realized_pnl=position.realized_pnl,
                unrealized_pnl=position.unrealized_pnl,
                source=position.source or payload.source,
                source_observed_at=position.observed_at or summary.observed_at,
            )
            for position in payload.positions
        ],
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
        order_snapshots = await container.repository.latest_broker_snapshots("orders")
        fill_snapshots = await container.repository.latest_broker_snapshots("fills")
        account_orders = [
            snapshot for snapshot in order_snapshots if snapshot.snapshot_key == container.account_key
        ]
        account_fills = [
            snapshot for snapshot in fill_snapshots if snapshot.snapshot_key == container.account_key
        ]
        if not account_orders or not account_fills:
            return unavailable_response()
        order_snapshot = max(account_orders, key=lambda item: item.observed_at)
        fill_snapshot = max(account_fills, key=lambda item: item.observed_at)
        order_payload = _OrdersPayload.model_validate(
            order_snapshot.raw_payload.get("normalized", order_snapshot.raw_payload)
        )
        fill_payload = _FillsPayload.model_validate(
            fill_snapshot.raw_payload.get("normalized", fill_snapshot.raw_payload)
        )
        now = container.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return unavailable_response()
        exchange_day = now.astimezone(ZoneInfo("Asia/Kolkata")).date()
    except (AttributeError, TypeError, ValueError, ValidationError):
        return unavailable_response()
    except Exception:
        return unavailable_response()
    orders = [
        order
        for order in order_payload.orders
        if order.created_at.astimezone(ZoneInfo("Asia/Kolkata")).date() == exchange_day
    ]
    fills_by_order: dict[str, list[_FillPayload]] = {order.order_id: [] for order in orders}
    for fill in fill_payload.fills:
        if fill.order_id in fills_by_order and (
            fill.filled_at.astimezone(ZoneInfo("Asia/Kolkata")).date() == exchange_day
        ):
            fills_by_order[fill.order_id].append(fill)
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
                source=order_snapshot.source,
                source_observed_at=order_snapshot.observed_at,
                fills=[
                    FillResponse(
                        fill_id=fill.fill_id,
                        order_id=fill.order_id,
                        instrument_token=fill.instrument_token,
                        quantity=fill.quantity,
                        price=fill.price,
                        source_filled_at=fill.filled_at,
                        source=fill_snapshot.source,
                        source_observed_at=fill_snapshot.observed_at,
                    )
                    for fill in fills_by_order[order.order_id]
                ],
            )
            for order in orders
        ],
        served_at=utc_now(),
        correlation_id=correlation_id(),
    )


__all__ = ["router"]
