from trading_system.config import Settings


def test_settings_never_render_secret_values() -> None:
    settings = Settings(
        _env_file=None,
        groww_totp_token="token-value",
        groww_totp_secret="totp-value",
        postgres_password="postgres-value",
        redis_password="redis-value",
    )

    rendered = repr(settings) + repr(settings.safe_view())
    for secret in ("token-value", "totp-value", "postgres-value", "redis-value"):
        assert secret not in rendered
    assert settings.safe_view()["groww_configured"] is True
    assert settings.safe_view()["retention_enabled"] is False
    assert (
        Settings(_env_file=None, groww_totp_token="", groww_totp_secret=" ").groww_configured
        is False
    )
