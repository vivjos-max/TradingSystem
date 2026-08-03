"""Provider-neutral immutable models for market and account observations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class MarketDataSource(StrEnum):
    """Origin of a market observation."""

    LIVE = "live"
    DELAYED = "delayed"
    HISTORICAL = "historical"


class ServiceHealthState(StrEnum):
    """Health state reported by a dependency."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ImmutableModel(BaseModel):
    """Base model that rejects timestamps without an explicit timezone."""

    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: object) -> object:
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("datetime values must be timezone-aware")
        if isinstance(value, datetime):
            return value.astimezone(UTC)
        return value


class Instrument(ImmutableModel):
    """Instrument-master metadata, retained without provider-specific behavior."""

    instrument_token: str
    symbol: str
    exchange: str
    instrument_type: str
    expiry: date | None = None
    expiry_at: datetime | None = None
    strike: Decimal | None = None
    option_type: str | None = None
    lot_size: int
    tick_size: Decimal
    is_reserved: bool = False
    is_buyable: bool = True


class Quote(ImmutableModel):
    instrument_token: str
    last_price: Decimal
    observed_at: datetime
    source: MarketDataSource
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    volume: int | None = None


class OptionContract(ImmutableModel):
    """An option instrument retained within a selected chain."""

    instrument: Instrument

    @property
    def instrument_token(self) -> str:
        return self.instrument.instrument_token

    @property
    def strike(self) -> Decimal | None:
        return self.instrument.strike

    @property
    def lot_size(self) -> int:
        return self.instrument.lot_size

    @property
    def tick_size(self) -> Decimal:
        return self.instrument.tick_size


class OptionChain(ImmutableModel):
    underlying: Instrument
    expiry: date
    contracts: tuple[OptionContract, ...]


class MarginSummary(ImmutableModel):
    available_cash: Decimal
    used_margin: Decimal
    collateral: Decimal = Decimal("0")
    observed_at: datetime | None = None


class Position(ImmutableModel):
    instrument: Instrument
    quantity: int
    average_price: Decimal
    last_price: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    observed_at: datetime | None = None


class Order(ImmutableModel):
    order_id: str
    instrument: Instrument
    side: str
    quantity: int
    status: str
    created_at: datetime
    updated_at: datetime
    limit_price: Decimal | None = None


class Fill(ImmutableModel):
    fill_id: str
    order_id: str
    instrument: Instrument
    quantity: int
    price: Decimal
    filled_at: datetime


class MarketObservation(ImmutableModel):
    instrument: Instrument
    quote: Quote
    observed_at: datetime


class Candle(ImmutableModel):
    instrument_token: str
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class ServiceHealth(ImmutableModel):
    service: str
    state: ServiceHealthState
    observed_at: datetime
    detail: str | None = None


class SystemStatus(ImmutableModel):
    observed_at: datetime
    services: tuple[ServiceHealth, ...]


class NiftyUniverse(ImmutableModel):
    """Read-only NIFTY index, derivative contracts, and deterministic strike window."""

    index: Instrument
    future: Instrument
    option_expiry: date
    atm_strike: Decimal
    selected_strikes: tuple[Decimal, ...]
    option_chain: OptionChain
