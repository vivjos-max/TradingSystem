from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .models import GlobalState, HealthState, SafeError
from .market import Session, SessionState
from .quality import assess_quality
from .store import Store, StoreUnavailableError


@dataclass(frozen=True)
class AppContainer:
    store: Any
    clock: Any = lambda: datetime.now(timezone.utc)


def _source_at(value: Any) -> datetime | None:
    raw = value.get("source_at") if isinstance(value, dict) else None
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(raw, datetime) or raw.tzinfo is None:
        return None
    return raw.astimezone(timezone.utc)


def _default_store() -> Store:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        user = quote(os.getenv("POSTGRES_USER", "trading_system"))
        password = quote(os.getenv("POSTGRES_PASSWORD", ""), safe="")
        host = os.getenv("POSTGRES_HOST", "postgres")
        port = os.getenv("POSTGRES_PORT", "5432")
        database = quote(os.getenv("POSTGRES_DB", "trading_system"))
        database_url = f"postgresql://{user}:{password}@{host}:{port}/{database}"
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        password = quote(os.getenv("REDIS_PASSWORD", ""), safe="")
        redis_url = f"redis://:{password}@{os.getenv('REDIS_HOST', 'redis')}:{os.getenv('REDIS_PORT', '6379')}/0"
    return Store(
        database_url,
        redis_url,
        timeout_seconds=int(os.getenv("DEPENDENCY_TIMEOUT_SECONDS", "2")),
    )


def create_app(container: AppContainer | Any | None = None) -> FastAPI:
    if container is None:
        container = AppContainer(_default_store())
    elif not isinstance(container, AppContainer):
        container = AppContainer(container)

    app = FastAPI(title="Trading System Read API", docs_url=None, redoc_url=None)
    quote_stale_seconds = int(os.getenv("QUOTE_STALE_SECONDS", "15"))
    option_stale_seconds = int(os.getenv("OPTION_STALE_SECONDS", "60"))
    max_clock_skew = timedelta(seconds=int(os.getenv("SOURCE_CLOCK_SKEW_SECONDS", "5")))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=[],
    )

    def read(kind: str, *, identifier: str | None = None) -> Any | None:
        return container.store.get_live_value(kind, identifier=identifier)

    def failure(subsystem: str, reason: str, last: datetime | str | None = None) -> JSONResponse:
        last_at = _source_at({"source_at": last}) if last is not None else None
        return JSONResponse(
            status_code=503,
            content=SafeError(
                code="LIVE_STATE_UNAVAILABLE",
                reason=reason,
                subsystem=subsystem,
                correlation_id=uuid4().hex,
                last_success_at=last_at,
            ).model_dump(mode="json"),
        )

    def required(kind: str, subsystem: str, *, identifier: str | None = None) -> Any | JSONResponse:
        try:
            value = read(kind, identifier=identifier)
        except StoreUnavailableError:
            return failure("stores", "REQUIRED_STORE_UNAVAILABLE")
        if value is None:
            return failure(subsystem, "REQUIRED_LIVE_STATE_UNAVAILABLE")
        if kind == "account":
            source_at = _source_at(value)
            if source_at is not None and source_at - container.clock() > max_clock_skew:
                return failure(subsystem, "ACCOUNT_TIMESTAMP_FUTURE", source_at)
            if source_at is None or container.clock() - source_at > timedelta(seconds=60):
                return failure(subsystem, "ACCOUNT_STATE_STALE", source_at)
        if kind == "option-chain":
            source_at = _source_at(value)
            if source_at is None or source_at - container.clock() > max_clock_skew:
                return failure(subsystem, "OPTION_TIMESTAMP_INVALID", source_at)
        return value

    def readiness() -> tuple[bool, list[dict[str, Any]]]:
        checks: list[dict[str, Any]] = []
        try:
            if hasattr(container.store, "healthcheck"):
                container.store.healthcheck()
            for subsystem in (
                "stores",
                "account_reconciliation",
                "subscriptions",
                "market",
                "feed",
            ):
                value = read("heartbeat", identifier=subsystem)
                checks.append(
                    value
                    or {
                        "subsystem": subsystem,
                        "state": HealthState.BLOCKED,
                        "reason": "NO_HEARTBEAT",
                    }
                )
            subscription = read("subscription")
            account = read("account")
            session = read("heartbeat", identifier="market_session")
            spot = read("quote", identifier=subscription.get("spot")) if subscription else None
            future = read("quote", identifier=subscription.get("future")) if subscription else None
            option_chain = read("option-chain")
        except StoreUnavailableError:
            return False, [
                {
                    "subsystem": "stores",
                    "state": HealthState.BLOCKED,
                    "reason": "REQUIRED_STORE_UNAVAILABLE",
                }
            ]

        now = container.clock()
        session_at = _source_at(session)
        if (
            not isinstance(session, dict)
            or str(session.get("state")) != HealthState.HEALTHY
            or session_at is None
            or session_at - now > max_clock_skew
        ):
            checks[3] = {
                **checks[3],
                "state": HealthState.BLOCKED,
                "reason": "MARKET_SESSION_UNAVAILABLE",
            }
        elif not subscription or not spot or not future or not option_chain:
            checks[3] = {
                **checks[3],
                "state": HealthState.BLOCKED,
                "reason": "REQUIRED_QUOTE_UNAVAILABLE",
            }
        else:
            report = market_quality(subscription, spot, future, option_chain, session)
            if report["status"] in {GlobalState.BLOCKED, GlobalState.STALE}:
                issue = report["issues"][0]
                checks[3] = {
                    **checks[3],
                    "state": report["status"],
                    "reason": f"{issue['component'].upper()}_{issue['code']}",
                }
        ready = all(str(item.get("state")) == HealthState.HEALTHY for item in checks)
        ready = ready and bool(subscription and subscription.get("active")) and account is not None
        account_at = _source_at(account)
        if account_at is None:
            checks[1] = {
                **checks[1],
                "state": HealthState.BLOCKED,
                "reason": "ACCOUNT_TIMESTAMP_MISSING",
            }
            ready = False
        elif account_at - now > max_clock_skew:
            checks[1] = {
                **checks[1],
                "state": HealthState.BLOCKED,
                "reason": "ACCOUNT_TIMESTAMP_FUTURE",
            }
            ready = False
        elif now - account_at > timedelta(seconds=60):
            checks[1] = {**checks[1], "state": HealthState.STALE, "reason": "ACCOUNT_STATE_STALE"}
            ready = False
        feed_at = _source_at(checks[-1])
        if feed_at is None or feed_at - now > max_clock_skew:
            checks[-1] = {
                **checks[-1],
                "state": HealthState.BLOCKED,
                "reason": "FEED_TIMESTAMP_INVALID",
            }
            ready = False
        elif now - feed_at > timedelta(seconds=10):
            checks[-1] = {**checks[-1], "state": HealthState.STALE, "reason": "FEED_STALE"}
            ready = False
        return ready, checks

    def market_quality(
        subscription: Any, spot: Any, future: Any, options: Any, session_value: Any
    ) -> dict[str, Any]:
        subscription = subscription if isinstance(subscription, dict) else {}
        options = options if isinstance(options, dict) else {}
        session_value = session_value if isinstance(session_value, dict) else {}
        if not session_value or str(session_value.get("state")) == HealthState.BLOCKED:
            session = Session(SessionState.BLOCKED, "MARKET_SESSION_UNAVAILABLE")
        elif session_value.get("reason") == "MARKET_CLOSED":
            session = Session(SessionState.CLOSED, "MARKET_CLOSED")
        else:
            session = Session(SessionState.OPEN)
        report = assess_quality(
            session=session,
            now=container.clock(),
            timestamps={
                "spot": _source_at(spot),
                "future": _source_at(future),
                "options": _source_at(options),
            },
            thresholds={
                "spot": quote_stale_seconds,
                "future": quote_stale_seconds,
                "options": option_stale_seconds,
            },
            required=("spot", "future", "options"),
            feed_generation=subscription.get("feed_generation") or options.get("feed_generation"),
            max_future_seconds=int(max_clock_skew.total_seconds()),
        )
        return report.model_dump(mode="json")

    @app.get("/health/live")
    def live() -> dict[str, Any]:
        return {"status": "alive", "source": "PROCESS", "source_at": container.clock()}

    @app.get("/health/ready")
    def ready() -> Any:
        is_ready, checks = readiness()
        if not is_ready:
            last = next(
                (item.get("source_at") for item in reversed(checks) if item.get("source_at")), None
            )
            return failure("readiness", "STARTUP_OR_LIVE_PREREQUISITE_UNAVAILABLE", last)
        return {"ready": True, "source": "SHARED_LIVE_STATE", "source_at": container.clock()}

    @app.get("/api/v1/system/status")
    def status() -> dict[str, Any]:
        is_ready, checks = readiness()
        states = [str(item.get("state")) for item in checks]
        try:
            session = read("heartbeat", identifier="market_session") or {}
            depth = read("heartbeat", identifier="depth") or {}
        except StoreUnavailableError:
            session = {"state": HealthState.BLOCKED, "reason": "REQUIRED_STORE_UNAVAILABLE"}
            depth = {}
        required_states = [
            str(item.get("state")) for item in checks if item.get("subsystem") != "feed"
        ]
        required_snapshots_healthy = all(state == HealthState.HEALTHY for state in required_states)
        depth_state = str(depth.get("state", HealthState.HEALTHY))
        if (
            str(session.get("state")) == HealthState.BLOCKED
            or HealthState.BLOCKED in required_states
        ):
            state = GlobalState.BLOCKED
        elif HealthState.STALE in required_states:
            state = GlobalState.STALE
        elif HealthState.DEGRADED in required_states or depth_state == HealthState.DEGRADED:
            state = GlobalState.DEGRADED
        elif session.get("reason") == "MARKET_CLOSED" and required_snapshots_healthy:
            state = GlobalState.MARKET_CLOSED
        elif HealthState.BLOCKED in states:
            state = GlobalState.BLOCKED
        elif HealthState.STALE in states:
            state = GlobalState.STALE
        elif is_ready:
            state = GlobalState.LIVE
        else:
            state = GlobalState.BLOCKED
        return {
            "state": state,
            "observed_at": container.clock(),
            "subsystems": [*checks, *([depth] if depth else []), *([session] if session else [])],
            "source": "SHARED_LIVE_STATE",
            "source_at": container.clock(),
        }

    @app.get("/api/v1/market/nifty")
    def nifty() -> Any:
        subscription = required("subscription", "subscriptions")
        if isinstance(subscription, JSONResponse):
            return subscription
        spot = required("quote", "market", identifier=subscription.get("spot"))
        if isinstance(spot, JSONResponse):
            return spot
        future = required("quote", "market", identifier=subscription.get("future"))
        if isinstance(future, JSONResponse):
            return future
        try:
            session = read("heartbeat", identifier="market_session")
        except StoreUnavailableError:
            return failure("stores", "REQUIRED_STORE_UNAVAILABLE")
        session_at = _source_at(session)
        if (
            not session
            or str(session.get("state")) != HealthState.HEALTHY
            or session_at is None
            or session_at - container.clock() > max_clock_skew
        ):
            return failure("market", "MARKET_SESSION_UNAVAILABLE")
        options_value = required("option-chain", "options")
        if isinstance(options_value, JSONResponse):
            return options_value
        quality = market_quality(subscription, spot, future, options_value, session)
        if quality["status"] in {GlobalState.BLOCKED, GlobalState.STALE}:
            issue = quality["issues"][0]
            if issue["component"] in {"spot", "future"}:
                reason = {
                    "TIMESTAMP_FUTURE": "QUOTE_TIMESTAMP_FUTURE",
                    "MISSING": "QUOTE_TIMESTAMP_MISSING",
                    "STALE": "QUOTE_STALE",
                }[issue["code"]]
            else:
                reason = f"OPTION_{issue['code']}"
            return failure("market", reason)
        source_at = max(spot["source_at"], future["source_at"])
        return {
            "spot": spot,
            "future": future,
            "quality": quality,
            "source": "SHARED_LIVE_STATE",
            "source_at": source_at,
        }

    @app.get("/api/v1/market/nifty/options")
    def options() -> Any:
        value = required("option-chain", "options")
        if isinstance(value, JSONResponse):
            return value
        try:
            subscription = read("subscription")
            session = read("heartbeat", identifier="market_session")
            spot = read("quote", identifier=subscription.get("spot")) if subscription else None
            future = read("quote", identifier=subscription.get("future")) if subscription else None
        except StoreUnavailableError:
            return failure("stores", "REQUIRED_STORE_UNAVAILABLE")
        if subscription and spot and future:
            quality = market_quality(subscription, spot, future, value, session)
            if quality["status"] in {GlobalState.BLOCKED, GlobalState.STALE}:
                issue = quality["issues"][0]
                return failure("options", f"{issue['component'].upper()}_{issue['code']}")
        contracts = value.get("contracts", ())
        if contracts and isinstance(contracts[0], str):
            assembled = []
            for symbol in contracts:
                contract = required("option-contract", "options", identifier=symbol)
                if isinstance(contract, JSONResponse):
                    return contract
                assembled.append(contract)
            contracts = assembled
        return {
            **value,
            "contracts": contracts,
            "source": value.get("source", "SHARED_LIVE_STATE"),
        }

    @app.get("/api/v1/market/nifty/candles")
    def candles(interval: Literal["1m", "5m", "15m"] = Query(...)) -> Any:
        subscription = required("subscription", "subscriptions")
        if isinstance(subscription, JSONResponse):
            return subscription
        try:
            values = container.store.read_candles(subscription["spot"], interval, limit=100)
        except StoreUnavailableError:
            return failure("stores", "REQUIRED_STORE_UNAVAILABLE")
        if not values:
            return failure("candles", "REQUIRED_LIVE_STATE_UNAVAILABLE")
        return {
            "interval": interval,
            "candles": [
                value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                for value in values
            ],
            "source": "DURABLE_STORE",
            "source_at": values[-1].bucket_start
            if hasattr(values[-1], "bucket_start")
            else values[-1]["bucket_start"],
        }

    @app.get("/api/v1/account/summary")
    def account_summary() -> Any:
        value = required("account", "account")
        if isinstance(value, JSONResponse):
            return value
        return {**value["summary"], "source": value["source"], "source_at": value["source_at"]}

    @app.get("/api/v1/account/orders")
    def account_orders() -> Any:
        value = required("account", "account")
        if isinstance(value, JSONResponse):
            return value
        return {
            "orders": value["orders"],
            "source": value["source"],
            "source_at": value["source_at"],
        }

    return app


app = create_app()
