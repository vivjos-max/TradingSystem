"""Deterministic selection of the NIFTY instrument universe."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_system.domain.models import Instrument, NiftyUniverse, OptionChain, OptionContract


EXCHANGE_TIMEZONE = ZoneInfo("Asia/Kolkata")
NIFTY_SYMBOL = "NIFTY"
INDEX_TYPE = "INDEX"
FUTURE_TYPE = "FUTIDX"
OPTION_TYPE = "OPTIDX"
STRIKES_EACH_SIDE = 5


def resolve_nifty_universe(
    instruments: tuple[Instrument, ...] | list[Instrument],
    spot: Decimal,
    as_of: datetime,
) -> NiftyUniverse:
    """Select NIFTY contracts using only eligible master-data entries.

    Expiry selection follows the exchange's local date while all supplied
    observation timestamps must remain explicitly timezone-aware.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")

    exchange_date = as_of.astimezone(EXCHANGE_TIMEZONE).date()
    nifty_index = _first_index(instruments)
    future = _nearest_derivative(instruments, FUTURE_TYPE, exchange_date)
    option_expiry = _nearest_option_expiry(instruments, exchange_date)
    eligible_options = tuple(
        instrument
        for instrument in instruments
        if _is_eligible_option(instrument, option_expiry, exchange_date)
    )
    strikes = tuple(sorted({instrument.strike for instrument in eligible_options if instrument.strike is not None}))
    if not strikes:
        raise ValueError("no eligible NIFTY option strikes for selected expiry")

    atm_strike = min(strikes, key=lambda strike: (abs(strike - spot), strike))
    atm_index = strikes.index(atm_strike)
    selected_strikes = strikes[max(0, atm_index - STRIKES_EACH_SIDE) : atm_index + STRIKES_EACH_SIDE + 1]
    selected_contracts = tuple(
        OptionContract(instrument=instrument)
        for instrument in eligible_options
        if instrument.strike in selected_strikes
    )
    return NiftyUniverse(
        index=nifty_index,
        future=future,
        option_expiry=option_expiry,
        atm_strike=atm_strike,
        selected_strikes=selected_strikes,
        option_chain=OptionChain(
            underlying=nifty_index,
            expiry=option_expiry,
            contracts=selected_contracts,
        ),
    )


def _first_index(instruments: tuple[Instrument, ...] | list[Instrument]) -> Instrument:
    for instrument in instruments:
        if _is_nifty(instrument) and instrument.instrument_type.upper() == INDEX_TYPE:
            return instrument
    raise ValueError("NIFTY index instrument is required")


def _nearest_derivative(
    instruments: tuple[Instrument, ...] | list[Instrument],
    instrument_type: str,
    exchange_date: date,
) -> Instrument:
    candidates = tuple(
        instrument
        for instrument in instruments
        if _is_nifty(instrument)
        and instrument.instrument_type.upper() == instrument_type
        and _is_eligible_derivative(instrument, exchange_date)
    )
    if not candidates:
        raise ValueError(f"no eligible NIFTY {instrument_type} contract")
    return min(candidates, key=lambda instrument: (instrument.expiry, instrument.instrument_token))


def _nearest_option_expiry(
    instruments: tuple[Instrument, ...] | list[Instrument], exchange_date: date
) -> date:
    expiries = tuple(
        instrument.expiry
        for instrument in instruments
        if _is_nifty(instrument)
        and instrument.instrument_type.upper() == OPTION_TYPE
        and _is_eligible_derivative(instrument, exchange_date)
        and instrument.strike is not None
    )
    if not expiries:
        raise ValueError("no eligible NIFTY option contract")
    return min(expiry for expiry in expiries if expiry is not None)


def _is_eligible_option(instrument: Instrument, expiry: date, exchange_date: date) -> bool:
    return (
        _is_nifty(instrument)
        and instrument.instrument_type.upper() == OPTION_TYPE
        and instrument.expiry == expiry
        and instrument.strike is not None
        and _is_eligible_derivative(instrument, exchange_date)
    )


def _is_eligible_derivative(instrument: Instrument, exchange_date: date) -> bool:
    return (
        instrument.expiry is not None
        and instrument.expiry >= exchange_date
        and not instrument.is_reserved
        and instrument.is_buyable
    )


def _is_nifty(instrument: Instrument) -> bool:
    return instrument.symbol.strip().upper() == NIFTY_SYMBOL
