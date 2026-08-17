from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def normalize_datetimes_to_utc(cls, value: object) -> object:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("timestamps must be timezone-aware")
            return value.astimezone(timezone.utc)
        return value


class DataSource(StrEnum):
    GROWW_LIVE = "GROWW_LIVE"
    GROWW_HISTORICAL = "GROWW_HISTORICAL"
    LOCAL_RECONSTRUCTED = "LOCAL_RECONSTRUCTED"


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"


class GlobalState(StrEnum):
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    MARKET_CLOSED = "MARKET_CLOSED"


class Instrument(FrozenModel):
    trading_symbol: str
    exchange_token: str
    exchange: str
    segment: str
    instrument_type: str
    underlying_symbol: str | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: Literal["CE", "PE"] | None = None
    lot_size: int | None = Field(default=None, gt=0)
    tick_size: Decimal | None = Field(default=None, gt=0)


class NiftyUniverse(FrozenModel):
    spot: Instrument
    future: Instrument
    option_expiry: date
    atm_strike: Decimal
    options: tuple[Instrument, ...]


class MarketObservation(FrozenModel):
    observation_id: str
    instrument: str
    price: Decimal = Field(gt=0)
    event_at: datetime
    received_at: datetime
    cumulative_volume: Decimal | None = Field(default=None, ge=0)
    source: DataSource = DataSource.GROWW_LIVE

    @model_validator(mode="after")
    def require_aware_timestamps(self) -> "MarketObservation":
        if self.event_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("event_at and received_at must be timezone-aware")
        return self


class Candle(FrozenModel):
    instrument: str
    interval: Literal["1m", "5m"]
    bucket_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = Field(default=None, ge=0)
    source: DataSource = DataSource.LOCAL_RECONSTRUCTED
    finalized: bool = True
    observation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_candle(self) -> "Candle":
        if self.bucket_start.tzinfo is None:
            raise ValueError("bucket_start must be timezone-aware")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        return self


class Quote(FrozenModel):
    instrument: Instrument
    price: Decimal = Field(gt=0)
    source: DataSource
    source_at: datetime


class NiftySnapshot(FrozenModel):
    spot: Quote
    future: Quote | None = None


class OptionSnapshot(FrozenModel):
    instrument: Instrument
    ltp: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread: Decimal | None = None
    volume: Decimal | None = None
    open_interest: Decimal | None = None
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    source: DataSource
    source_at: datetime


class OptionWindow(FrozenModel):
    expiry: date
    atm_strike: Decimal
    contracts: tuple[OptionSnapshot, ...]
    source: DataSource = DataSource.GROWW_LIVE
    source_at: datetime


class Position(FrozenModel):
    trading_symbol: str
    quantity: Decimal
    average_price: Decimal | None = None
    last_price: Decimal | None = None
    pnl: Decimal | None = None


class AccountSummary(FrozenModel):
    available_margin: Decimal | None = None
    used_margin: Decimal | None = None
    positions: tuple[Position, ...] = ()
    source: DataSource = DataSource.GROWW_LIVE
    source_at: datetime


class ReconciledFill(FrozenModel):
    trade_id: str
    order_id: str
    segment: str
    quantity: Decimal
    price: Decimal
    source_at: datetime


class ReconciledOrder(FrozenModel):
    order_id: str
    segment: str
    trading_symbol: str
    status: str
    quantity: Decimal
    price: Decimal | None = None
    source_at: datetime
    fills: tuple[ReconciledFill, ...] = ()


class ServiceHealth(FrozenModel):
    subsystem: str
    state: HealthState
    reason: str | None = None
    last_success_at: datetime | None = None


class SafeError(FrozenModel):
    code: str
    reason: str
    subsystem: str
    correlation_id: str
    last_success_at: datetime | None = None


class SystemStatus(FrozenModel):
    state: GlobalState
    market_open: bool
    observed_at: datetime
    subsystems: tuple[ServiceHealth, ...]
    reason: str | None = None
