import pytest

from trading_system.settings import Settings, SettingsValidationError, get_settings


def test_safe_view_never_contains_groww_secrets(monkeypatch):
    """A safe API/log representation must not leak configured Groww credentials."""
    monkeypatch.setenv("GROWW_TOTP_TOKEN", "test-token-sensitive")
    monkeypatch.setenv("GROWW_TOTP_SECRET", "test-secret-sensitive")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-postgres-password-sensitive")
    monkeypatch.setenv("REDIS_PASSWORD", "test-redis-password-sensitive")

    settings = Settings(_env_file=None)
    rendered = settings.safe_view().model_dump_json()

    assert "test-token-sensitive" not in rendered
    assert "test-secret-sensitive" not in rendered
    assert "test-postgres-password-sensitive" not in rendered
    assert "test-redis-password-sensitive" not in rendered
    assert "test-token-sensitive" not in repr(settings)
    assert "test-secret-sensitive" not in repr(settings)
    assert "test-postgres-password-sensitive" not in repr(settings)
    assert "test-redis-password-sensitive" not in repr(settings)
    assert settings.safe_view().groww_credentials_configured is True


def test_get_settings_redacts_sensitive_validation_input(monkeypatch):
    """Settings failures expose diagnostics without serializing raw environment values."""
    sentinel = "validation-input-sensitive"
    monkeypatch.setenv("GROWW_TOTP_TOKEN", "validation-token-sensitive")
    monkeypatch.setenv("GROWW_TOTP_SECRET", "validation-secret-sensitive")
    monkeypatch.setenv("POSTGRES_PASSWORD", "validation-postgres-sensitive")
    monkeypatch.setenv("REDIS_PASSWORD", "validation-redis-sensitive")
    monkeypatch.setenv("POSTGRES_PORT", sentinel)
    get_settings.cache_clear()

    with pytest.raises(SettingsValidationError) as caught:
        get_settings()

    error = caught.value
    rendered = str(error)
    serialized = error.safe_view().model_dump_json()
    for secret in (
        sentinel,
        "validation-token-sensitive",
        "validation-secret-sensitive",
        "validation-postgres-sensitive",
        "validation-redis-sensitive",
    ):
        assert secret not in rendered
        assert secret not in serialized
    assert "postgres_port" in rendered
    assert "int_parsing" in serialized


def test_api_accepts_explicit_local_cors_origins_without_groww_credentials(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS",
        '["http://127.0.0.1:3000","http://localhost:3000"]',
    )
    monkeypatch.delenv("GROWW_TOTP_TOKEN", raising=False)
    monkeypatch.delenv("GROWW_TOTP_SECRET", raising=False)

    settings = Settings(_env_file=None)

    assert settings.cors_origins == (
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    )
    assert settings.safe_view().groww_credentials_configured is False
