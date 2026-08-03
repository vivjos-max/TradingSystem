import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from trading_system.api.runtime import build_runtime_app
from trading_system.settings import Settings


def test_api_runtime_composes_only_database_redis_and_safe_dashboard_state() -> None:
    settings = Settings(
        postgres_host="postgres",
        postgres_password="local-only",
        redis_host="redis",
        api_host="0.0.0.0",
    )
    app = build_runtime_app(settings)
    container = app.state.container

    assert container.nifty_future_instrument_key == "nifty-nearest-future"
    assert container.repository.__class__.__name__ == "PostgresRepository"
    assert container.live_state.__class__.__name__ == "RedisLiveState"
    assert not hasattr(container, "broker")


def test_importing_api_runtime_does_not_load_worker_or_broker_modules() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(backend_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; import trading_system.api.runtime; "
                "print(json.dumps(sorted(sys.modules)))"
            ),
        ],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    imported_modules = set(json.loads(result.stdout))

    assert "trading_system.worker.runtime" not in imported_modules
    assert not any(name.startswith("trading_system.brokers") for name in imported_modules)
    assert "growwapi" not in imported_modules


def test_api_lifespan_closes_redis_and_disposes_the_database_engine() -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    class FakeRedis:
        def __init__(self) -> None:
            self.closed = False

        async def get(self, _: str) -> None:
            return None

        async def aclose(self) -> None:
            self.closed = True

    engine = FakeEngine()
    redis_client = FakeRedis()
    settings = Settings(
        postgres_host="postgres",
        postgres_password="local-only",
        redis_host="redis",
        api_host="0.0.0.0",
    )
    app = build_runtime_app(
        settings,
        engine_factory=lambda *_args, **_kwargs: engine,
        redis_factory=lambda *_args, **_kwargs: redis_client,
    )

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(run_lifespan())

    assert redis_client.closed is True
    assert engine.disposed is True
