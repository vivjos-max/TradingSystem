"""Safe system-status view assembled from persisted health transitions."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from trading_system.api.dependencies import AppContainer
from trading_system.api.routes.health import (
    SafeUnavailableResponse,
    correlation_id,
    get_container,
    unavailable_response,
    utc_now,
)


class ServiceResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    service: str
    state: str
    source_observed_at: datetime


class SystemStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: str
    source_observed_at: datetime
    services: list[ServiceResponse]
    served_at: datetime
    correlation_id: str


_SEVERITY = ("blocked", "stale", "degraded", "market_closed", "live")


def _system_state(states: list[str]) -> str:
    lowered = {state.lower() for state in states}
    return next((state for state in _SEVERITY if state in lowered), "degraded")


router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get(
    "/status",
    response_model=SystemStatusResponse,
    responses={503: {"model": SafeUnavailableResponse}},
)
async def status(
    container: AppContainer = Depends(get_container),
) -> SystemStatusResponse | object:
    if not container.readiness.is_ready:
        return unavailable_response()
    try:
        transitions = await container.repository.latest_health_transitions()
    except Exception:
        return unavailable_response()
    if not transitions:
        return unavailable_response()
    services = [
        ServiceResponse(
            service=transition.service,
            state=transition.state,
            source_observed_at=transition.observed_at,
        )
        for transition in transitions
    ]
    source_observed_at = max(service.source_observed_at for service in services)
    return SystemStatusResponse(
        state=_system_state([service.state for service in services]),
        source_observed_at=source_observed_at,
        services=services,
        served_at=utc_now(),
        correlation_id=correlation_id(),
    )


__all__ = ["SystemStatusResponse", "router"]
