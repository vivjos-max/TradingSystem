"""Provider-neutral read-only broker port and safe failure model."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from trading_system.domain.models import (
    Fill,
    Instrument,
    MarginSummary,
    OptionChain,
    OptionContract,
    OptionGreeks,
    Order,
    Position,
    Quote,
)


class BrokerUnavailableView(BaseModel):
    """Public broker failure details with no raw upstream data."""

    model_config = ConfigDict(frozen=True)

    code: str
    correlation_id: str
    summary: str


class BrokerUnavailable(RuntimeError):
    """Stable, secret-safe failure raised at the broker boundary."""

    def __init__(self, *, code: str, correlation_id: str, summary: str) -> None:
        self.code = code
        self.correlation_id = correlation_id
        self.summary = summary
        self._safe_view = BrokerUnavailableView(
            code=code,
            correlation_id=correlation_id,
            summary=summary,
        )
        super().__init__(f"{code}: {summary} (correlation_id={correlation_id})")

    def safe_view(self) -> BrokerUnavailableView:
        """Return the only structured representation exposed to consumers."""
        return self._safe_view


class BrokerReadPort(Protocol):
    """Exact read surface available to workers and services."""

    def instruments(self) -> tuple[Instrument, ...]: ...

    def nifty_quote(self) -> Quote: ...

    def option_chain(self, expiry: date) -> OptionChain: ...

    def greeks(self, contracts: tuple[OptionContract, ...]) -> tuple[OptionGreeks, ...]: ...

    def margin(self) -> MarginSummary: ...

    def positions(self) -> tuple[Position, ...]: ...

    def orders(self) -> tuple[Order, ...]: ...

    def fills(self, orders: tuple[Order, ...]) -> tuple[Fill, ...]: ...
