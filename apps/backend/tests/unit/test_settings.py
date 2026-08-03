from trading_system.settings import Settings


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
