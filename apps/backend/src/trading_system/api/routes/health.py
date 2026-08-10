"""Liveness and readiness endpoints that disclose only safe operational state."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from trading_system.api.dependencies import AppContainer


class LiveResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "live"
    observed_at: datetime
    correlation_id: str


class SafeUnavailableResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = "LIVE_STATE_UNAVAILABLE"
    correlation_id: str
    occurred_at: datetime


class ReadinessUnavailable(Exception):
    """The shared readiness state cannot safely authorize a data response."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def correlation_id() -> str:
    return str(uuid4())


def unavailable_response() -> JSONResponse:
    payload = SafeUnavailableResponse(
        correlation_id=correlation_id(), occurred_at=utc_now()
    )
    return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))


def get_container(request: Request) -> AppContainer:
    return request.app.state.container  # type: ignore[no-any-return]


async def get_ready_container(request: Request) -> AppContainer:
    """Refresh separate-process readiness before each protected browser request."""
    container = get_container(request)
    try:
        await container.readiness.restore()
    except Exception as error:
        raise ReadinessUnavailable from error
    if not container.readiness.is_ready:
        raise ReadinessUnavailable
    return container


router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(observed_at=utc_now(), correlation_id=correlation_id())


@router.head("/health/live", include_in_schema=True)
async def live_head() -> Response:
    return Response(status_code=200)


@router.get(
    "/health/ready",
    response_model=LiveResponse,
    responses={503: {"model": SafeUnavailableResponse}},
)
async def ready(container: AppContainer = Depends(get_ready_container)) -> LiveResponse:
    del container
    return LiveResponse(status="ready", observed_at=utc_now(), correlation_id=correlation_id())


@router.head("/health/ready", include_in_schema=True)
async def ready_head(container: AppContainer = Depends(get_ready_container)) -> Response:
    del container
    return Response(status_code=200)


__all__ = [
    "SafeUnavailableResponse",
    "ReadinessUnavailable",
    "correlation_id",
    "get_container",
    "get_ready_container",
    "router",
    "unavailable_response",
    "utc_now",
]
