"""FastAPI application factory for the localhost read-only dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from trading_system.api.dependencies import AppContainer
from trading_system.api.routes.account import router as account_router
from trading_system.api.routes.health import (
    ReadinessUnavailable,
    router as health_router,
    unavailable_response,
)
from trading_system.api.routes.market import router as market_router
from trading_system.api.routes.system import router as system_router


_LOCAL_DASHBOARD_ORIGINS = ("http://127.0.0.1:3000", "http://localhost:3000")


def create_app(container: AppContainer) -> FastAPI:
    """Create an API that can observe injected state but cannot execute trades."""
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await container.readiness.restore()
        except Exception:
            # A cache/network failure leaves the coordinator not-ready; routes expose only
            # the structured 503 response instead of process-startup exception details.
            pass
        try:
            yield
        finally:
            if container.shutdown is not None:
                await container.shutdown()

    app = FastAPI(
        title="Trading System Read-Only API", version="0.1.0", lifespan=lifespan
    )
    app.state.container = container

    @app.exception_handler(ReadinessUnavailable)
    async def readiness_unavailable(_: Request, __: ReadinessUnavailable) -> JSONResponse:
        return unavailable_response()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(
            getattr(container, "cors_origins", _LOCAL_DASHBOARD_ORIGINS)
        ),
        allow_credentials=False,
        allow_methods=["GET", "HEAD"],
        allow_headers=[],
    )
    app.include_router(health_router)
    app.include_router(system_router)
    app.include_router(market_router)
    app.include_router(account_router)
    return app


__all__ = ["create_app"]
