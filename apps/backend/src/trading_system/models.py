from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

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
    LIVE_ACCEPTED = "LIVE_ACCEPTED"
    DURABLE_RECONSTRUCTED = "DURABLE_RECONSTRUCTED"
    LOCAL_RECONSTRUCTED = "LOCAL_RECONSTRUCTED"


class EventOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    INACTIVE_FEED_GENERATION = "INACTIVE_FEED_GENERATION"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    MARKET_CLOSED = "MARKET_CLOSED"
    MALFORMED = "MALFORMED"
    MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"
    OTHER = "OTHER"


class CandleCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


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
    groww_symbol: str | None = None
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
    feed_generation: str | None = None

    @model_validator(mode="after")
    def require_aware_timestamps(self) -> "MarketObservation":
        if self.event_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("event_at and received_at must be timezone-aware")
        return self


class Candle(FrozenModel):
    instrument: str
    interval: Literal["1m", "5m", "15m"]
    bucket_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = Field(default=None, ge=0)
    source: DataSource = DataSource.LOCAL_RECONSTRUCTED
    finalized: bool = True
    completeness: CandleCompleteness = CandleCompleteness.COMPLETE
    expected_components: int = 1
    actual_components: int = 1
    source_event_at_min: datetime | None = None
    source_event_at_max: datetime | None = None
    observation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_candle(self) -> "Candle":
        if self.bucket_start.tzinfo is None:
            raise ValueError("bucket_start must be timezone-aware")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if (
            self.expected_components < 1
            or not 0 < self.actual_components <= self.expected_components
        ):
            raise ValueError("candle component counts are invalid")
        if self.completeness is CandleCompleteness.COMPLETE and (
            self.actual_components != self.expected_components or not self.finalized
        ):
            raise ValueError("complete candles require every component and finalization")
        return self


class PersistedEvent(FrozenModel):
    kind: str
    source: str
    idempotency_key: str
    event_identity: str
    event_at: datetime
    received_at: datetime
    processed_at: datetime
    raw: dict[str, Any]
    normalized: dict[str, Any]
    outcome: EventOutcome
    feed_generation: str | None = None
    rejection_detail: str | None = None


class TimedValue(FrozenModel):
    value: Any = None
    source: DataSource
    source_at: datetime


class OptionContractState(FrozenModel):
    instrument: Instrument
    ltp: TimedValue | None = None
    bid: TimedValue | None = None
    ask: TimedValue | None = None
    depth: TimedValue | None = None
    open_interest: TimedValue | None = None
    implied_volatility: TimedValue | None = None
    delta: TimedValue | None = None
    gamma: TimedValue | None = None
    theta: TimedValue | None = None
    vega: TimedValue | None = None
    volume: TimedValue | None = None
    option_chain_snapshot: TimedValue | None = None


class DataQualityIssue(FrozenModel):
    component: str
    code: str
    age_ms: int | None = None


class DataQualityReport(FrozenModel):
    status: GlobalState
    as_of: datetime
    feed_generation: str | None = None
    ages_ms: dict[str, int | None]
    issues: tuple[DataQualityIssue, ...] = ()


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
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl_source: Literal["BROKER", "DERIVED"] | None = None
    unrealized_pnl_source: Literal["BROKER", "DERIVED"] | None = None


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
    source_timezone_assumed: bool = False


class ReconciledOrder(FrozenModel):
    order_id: str
    segment: str
    trading_symbol: str
    status: str
    quantity: Decimal
    price: Decimal | None = None
    source_at: datetime
    source_timezone_assumed: bool = False
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
