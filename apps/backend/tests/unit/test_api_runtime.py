from trading_system.api.runtime import build_runtime_app
from trading_system.settings import Settings
from trading_system.worker.runtime import NEAREST_FUTURE_QUOTE_KEY


def test_api_runtime_composes_only_database_redis_and_safe_dashboard_state() -> None:
    settings = Settings(
        postgres_host="postgres",
        postgres_password="local-only",
        redis_host="redis",
        api_host="0.0.0.0",
    )
    app = build_runtime_app(settings)
    container = app.state.container

    assert container.nifty_future_instrument_key == NEAREST_FUTURE_QUOTE_KEY
    assert container.repository.__class__.__name__ == "PostgresRepository"
    assert container.live_state.__class__.__name__ == "RedisLiveState"
    assert not hasattr(container, "broker")
