"""Secret-safe server-side settings for the read-only backend."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import BaseModel, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SafeSettingsView(BaseModel):
    """Configuration suitable for logs and API-facing diagnostics."""

    api_host: str
    api_port: int
    market_timezone: str
    feed_stale_seconds: int
    quote_stale_seconds: int
    account_stale_seconds: int
    dependency_timeout_seconds: int
    option_strikes_each_side: int
    groww_credentials_configured: bool


class SafeSettingsValidationIssue(BaseModel):
    """A field/type diagnostic with no source value attached."""

    field: str
    error_type: str


class SafeSettingsValidationView(BaseModel):
    """A settings loading failure that is safe to log or return from an API."""

    code: str = "SETTINGS_VALIDATION_FAILED"
    issues: tuple[SafeSettingsValidationIssue, ...]


class SettingsValidationError(RuntimeError):
    """Safe settings-loading failure that deliberately drops raw input values."""

    def __init__(self, issues: tuple[SafeSettingsValidationIssue, ...]) -> None:
        self._safe_view = SafeSettingsValidationView(issues=issues)
        details = "; ".join(f"{issue.field} ({issue.error_type})" for issue in issues)
        super().__init__(f"Settings validation failed: {details}")

    @classmethod
    def from_validation_error(cls, error: ValidationError) -> SettingsValidationError:
        """Retain only stable field/type diagnostics from Pydantic's error details."""
        issues = tuple(
            SafeSettingsValidationIssue(
                field=".".join(str(part) for part in detail["loc"]),
                error_type=str(detail["type"]),
            )
            for detail in error.errors()
        )
        return cls(issues)

    def safe_view(self) -> SafeSettingsValidationView:
        """Return the API-facing representation without Pydantic's raw input field."""
        return self._safe_view


class Settings(BaseSettings):
    """Server-only configuration with secrets protected from representations."""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="forbid",
        hide_input_in_errors=True,
    )

    groww_totp_token: SecretStr | None = None
    groww_totp_secret: SecretStr | None = None
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str = "trading_system"
    postgres_user: str = "trading_system"
    postgres_password: SecretStr | None = None
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: SecretStr | None = None
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    market_timezone: str = "Asia/Kolkata"
    feed_stale_seconds: int = 10
    quote_stale_seconds: int = 15
    account_stale_seconds: int = 60
    dependency_timeout_seconds: int = 2
    option_strikes_each_side: int = 5

    @field_validator(
        "groww_totp_token",
        "groww_totp_secret",
        "postgres_password",
        "redis_password",
        mode="before",
    )
    @classmethod
    def empty_secret_is_unconfigured(cls, value: object) -> object:
        """Treat blank values in the checked-in template as absent credentials."""
        if isinstance(value, str) and not value:
            return None
        return value

    @property
    def database_url(self) -> str:
        """Build the database URL only inside the server process."""
        password = self._secret_value(self.postgres_password)
        credentials = quote_plus(self.postgres_user)
        if password is not None:
            credentials = f"{credentials}:{quote_plus(password)}"
        return (
            f"postgresql+asyncpg://{credentials}@{self.postgres_host}:"
            f"{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        """Build the Redis URL only inside the server process."""
        password = self._secret_value(self.redis_password)
        credentials = f":{quote_plus(password)}@" if password is not None else ""
        return f"redis://{credentials}{self.redis_host}:{self.redis_port}/0"

    def safe_view(self) -> SafeSettingsView:
        """Return the non-sensitive configuration available to diagnostics."""
        return SafeSettingsView(
            api_host=self.api_host,
            api_port=self.api_port,
            market_timezone=self.market_timezone,
            feed_stale_seconds=self.feed_stale_seconds,
            quote_stale_seconds=self.quote_stale_seconds,
            account_stale_seconds=self.account_stale_seconds,
            dependency_timeout_seconds=self.dependency_timeout_seconds,
            option_strikes_each_side=self.option_strikes_each_side,
            groww_credentials_configured=(
                self._secret_value(self.groww_totp_token) is not None
                and self._secret_value(self.groww_totp_secret) is not None
            ),
        )

    @staticmethod
    def _secret_value(value: SecretStr | None) -> str | None:
        return value.get_secret_value() if value is not None else None


@lru_cache
def get_settings() -> Settings:
    """Load settings once per server process from the ignored .env file."""
    try:
        return Settings()
    except ValidationError as error:
        raise SettingsValidationError.from_validation_error(error) from None
