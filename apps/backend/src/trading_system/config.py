from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    groww_totp_token: SecretStr | None = None
    groww_totp_secret: SecretStr | None = None
    groww_feed_credential_dir: Path = Path("/run/groww")

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "trading_system"
    postgres_user: str = "trading_system"
    postgres_password: SecretStr | None = None
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: SecretStr | None = None

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    feed_stale_seconds: int = 10
    quote_stale_seconds: int = 15
    source_clock_skew_seconds: int = 5
    account_stale_seconds: int = 60
    dependency_timeout_seconds: int = 2
    groww_request_timeout_seconds: int = 5
    option_window: int = 5

    @property
    def groww_configured(self) -> bool:
        return bool(
            self.groww_totp_token
            and self.groww_totp_token.get_secret_value().strip()
            and self.groww_totp_secret
            and self.groww_totp_secret.get_secret_value().strip()
        )

    def safe_view(self) -> dict[str, object]:
        return {
            "groww_configured": self.groww_configured,
            "postgres": f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}",
            "postgres_password_configured": self.postgres_password is not None,
            "redis": f"{self.redis_host}:{self.redis_port}",
            "redis_password_configured": self.redis_password is not None,
            "api": f"{self.api_host}:{self.api_port}",
            "feed_stale_seconds": self.feed_stale_seconds,
            "quote_stale_seconds": self.quote_stale_seconds,
            "source_clock_skew_seconds": self.source_clock_skew_seconds,
            "account_stale_seconds": self.account_stale_seconds,
            "groww_request_timeout_seconds": self.groww_request_timeout_seconds,
        }
