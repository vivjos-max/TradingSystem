"""Read-only broker snapshot and account reconciliation orchestration."""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from trading_system.brokers.ports import BrokerReadPort, BrokerUnavailable
from trading_system.domain.models import (
    Fill,
    Instrument,
    MarginSummary,
    OptionChain,
    Order,
    Position,
    Quote,
)


_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_MAX_JITTER_FRACTION = 0.2
_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class SnapshotBundle:
    instruments: tuple[Instrument, ...]
    market: Quote
    option: OptionChain
    margin: MarginSummary
    positions: tuple[Position, ...]
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    refreshed_at: datetime


class SnapshotService:
    """Refresh a complete snapshot without converting account failures to flat state."""

    def __init__(
        self,
        broker: BrokerReadPort,
        *,
        sleeper: Callable[[float], Awaitable[None] | None] = asyncio.sleep,
        jitter: Callable[[float], float] = lambda delay: random.uniform(
            -delay * _MAX_JITTER_FRACTION, delay * _MAX_JITTER_FRACTION
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_attempts: int | None = None,
    ) -> None:
        if max_attempts is not None and max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._broker = broker
        self._sleeper = sleeper
        self._jitter = jitter
        self._clock = clock
        self._max_attempts = max_attempts
        self._last_snapshot: SnapshotBundle | None = None

    @property
    def last_snapshot(self) -> SnapshotBundle | None:
        return self._last_snapshot

    async def refresh(self) -> SnapshotBundle:
        attempt = 0
        while True:
            try:
                snapshot = self._refresh_once()
            except BrokerUnavailable:
                attempt += 1
                if self._max_attempts is not None and attempt >= self._max_attempts:
                    raise
                delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
                jitter = self._jitter(delay)
                bounded_jitter = min(
                    max(jitter, -delay * _MAX_JITTER_FRACTION),
                    delay * _MAX_JITTER_FRACTION,
                )
                result = self._sleeper(delay + bounded_jitter)
                if inspect.isawaitable(result):
                    await result
                continue
            self._last_snapshot = snapshot
            return snapshot

    def _refresh_once(self) -> SnapshotBundle:
        instruments = self._broker.instruments()

        # Account reads form one reconciliation gate. A failure aborts the bundle.
        margin = self._broker.margin()
        positions = self._broker.positions()
        orders = self._broker.orders()
        fills = self._broker.fills(orders)

        market = self._broker.nifty_quote()
        option = self._broker.option_chain(
            _nearest_nifty_option_expiry(instruments, self._clock())
        )
        refreshed_at = self._clock()
        if refreshed_at.tzinfo is None or refreshed_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return SnapshotBundle(
            instruments=instruments,
            market=market,
            option=option,
            margin=margin,
            positions=positions,
            orders=orders,
            fills=fills,
            refreshed_at=refreshed_at.astimezone(UTC),
        )


def _nearest_nifty_option_expiry(
    instruments: tuple[Instrument, ...], as_of: datetime
) -> date:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    exchange_date = as_of.astimezone(_MARKET_TIMEZONE).date()
    expiries = {
        instrument.expiry
        for instrument in instruments
        if instrument.symbol.strip().upper() == "NIFTY"
        and instrument.instrument_type.strip().upper() == "OPTIDX"
        and instrument.expiry is not None
        and instrument.expiry >= exchange_date
        and not instrument.is_reserved
        and instrument.is_buyable
    }
    if not expiries:
        raise ValueError("no eligible NIFTY option expiry")
    return min(expiries)


__all__ = ["SnapshotBundle", "SnapshotService"]
