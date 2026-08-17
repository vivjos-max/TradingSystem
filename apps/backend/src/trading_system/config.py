from pathlib import Path

from pydantic import Field, SecretStr
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
    feed_stale_seconds: int = Field(default=10, gt=0)
    quote_stale_seconds: int = Field(default=15, gt=0)
    source_clock_skew_seconds: int = Field(default=5, gt=0)
    account_stale_seconds: int = Field(default=60, gt=0)
    option_stale_seconds: int = Field(default=60, gt=0)
    dependency_timeout_seconds: int = Field(default=2, gt=0)
    groww_request_timeout_seconds: int = Field(default=5, gt=0)
    option_window: int = Field(default=5, ge=1)
    ingestion_queue_capacity: int = Field(default=4096, gt=0)
    ingestion_batch_size: int = Field(default=100, gt=0)
    retention_enabled: bool = False

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
            "option_stale_seconds": self.option_stale_seconds,
            "groww_request_timeout_seconds": self.groww_request_timeout_seconds,
            "ingestion_queue_capacity": self.ingestion_queue_capacity,
            "ingestion_batch_size": self.ingestion_batch_size,
            "retention_enabled": self.retention_enabled,
        }
