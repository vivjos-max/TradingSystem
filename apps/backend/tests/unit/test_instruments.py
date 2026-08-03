from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system.domain.models import Instrument, MarketDataSource, OptionGreeks, Quote
from trading_system.market.instruments import resolve_nifty_universe


@pytest.fixture
def instruments() -> tuple[Instrument, ...]:
    current_expiry = date(2026, 8, 6)
    next_expiry = date(2026, 8, 13)
    strikes = tuple(Decimal(value) for value in range(24550, 25051, 50))
    options = tuple(
        Instrument(
            instrument_token=f"NIFTY{current_expiry:%y%m%d}{strike}{option_type}",
            symbol="NIFTY",
            exchange="NFO",
            instrument_type="OPTIDX",
            expiry=current_expiry,
            strike=strike,
            option_type=option_type,
            lot_size=75,
            tick_size=Decimal("0.05"),
        )
        for strike in strikes
        for option_type in ("CE", "PE")
    )
    return (
        Instrument(
            instrument_token="NIFTY50",
            symbol="NIFTY",
            exchange="NSE",
            instrument_type="INDEX",
            lot_size=1,
            tick_size=Decimal("0.05"),
        ),
        Instrument(
            instrument_token="NIFTY26AUGFUT",
            symbol="NIFTY",
            exchange="NFO",
            instrument_type="FUTIDX",
            expiry=date(2026, 8, 27),
            lot_size=75,
            tick_size=Decimal("0.05"),
        ),
        *options,
        Instrument(
            instrument_token="NIFTY26AUGFUTRESERVED",
            symbol="NIFTY",
            exchange="NFO",
            instrument_type="FUTIDX",
            expiry=date(2026, 8, 6),
            lot_size=50,
            tick_size=Decimal("0.10"),
            is_reserved=True,
        ),
        Instrument(
            instrument_token="NIFTY24800CEUNBUYABLE",
            symbol="NIFTY",
            exchange="NFO",
            instrument_type="OPTIDX",
            expiry=current_expiry,
            strike=Decimal("24800"),
            option_type="CE",
            lot_size=40,
            tick_size=Decimal("0.10"),
            is_buyable=False,
        ),
        Instrument(
            instrument_token="NIFTY24750PEEXPIRED",
            symbol="NIFTY",
            exchange="NFO",
            instrument_type="OPTIDX",
            expiry=date(2026, 7, 30),
            strike=Decimal("24750"),
            option_type="PE",
            lot_size=50,
            tick_size=Decimal("0.05"),
        ),
        Instrument(
            instrument_token="NIFTY26AUG13FUT",
            symbol="NIFTY",
            exchange="NFO",
            instrument_type="FUTIDX",
            expiry=date(2026, 8, 13),
            lot_size=50,
            tick_size=Decimal("0.10"),
        ),
        Instrument(
            instrument_token="NIFTY26AUG13CE24600",
            symbol="NIFTY",
            exchange="NFO",
            instrument_type="OPTIDX",
            expiry=next_expiry,
            strike=Decimal("24600"),
            option_type="CE",
            lot_size=50,
            tick_size=Decimal("0.05"),
        ),
    )


def test_resolve_nifty_universe_uses_actual_strike_ladder(instruments):
    universe = resolve_nifty_universe(
        instruments=instruments,
        spot=Decimal("24776.40"),
        as_of=datetime(2026, 8, 3, 4, 15, tzinfo=UTC),
    )

    assert universe.atm_strike == Decimal("24800")
    assert universe.selected_strikes == tuple(Decimal(v) for v in range(24550, 25051, 50))
    assert universe.option_expiry.isoformat() == "2026-08-06"


def test_resolve_nifty_universe_rolls_to_exchange_date_and_preserves_metadata(instruments):
    universe = resolve_nifty_universe(
        instruments=instruments,
        spot=Decimal("24775"),
        as_of=datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
    )

    assert universe.future.instrument_token == "NIFTY26AUG13FUT"
    assert universe.future.expiry == date(2026, 8, 13)
    assert universe.future.lot_size == 50
    assert universe.future.tick_size == Decimal("0.10")
    assert universe.option_expiry == date(2026, 8, 13)
    assert universe.selected_strikes == (Decimal("24600"),)


def test_resolve_nifty_universe_excludes_reserved_non_buyable_and_expired_contracts(instruments):
    universe = resolve_nifty_universe(
        instruments=instruments,
        spot=Decimal("24800"),
        as_of=datetime(2026, 8, 3, 4, 15, tzinfo=UTC),
    )

    tokens = {contract.instrument_token for contract in universe.option_chain.contracts}
    assert "NIFTY24800CEUNBUYABLE" not in tokens
    assert "NIFTY24750PEEXPIRED" not in tokens
    assert "NIFTY26AUGFUTRESERVED" != universe.future.instrument_token
    assert all(contract.lot_size == 75 for contract in universe.option_chain.contracts)
    assert all(contract.tick_size == Decimal("0.05") for contract in universe.option_chain.contracts)


def test_resolve_nifty_universe_breaks_atm_ties_to_the_lower_eligible_strike(instruments):
    universe = resolve_nifty_universe(
        instruments=instruments,
        spot=Decimal("24775"),
        as_of=datetime(2026, 8, 3, 4, 15, tzinfo=UTC),
    )

    assert universe.atm_strike == Decimal("24750")


def test_instrument_rejects_naive_expiry_timestamp_and_is_immutable():
    instrument = Instrument(
        instrument_token="NIFTY50",
        symbol="NIFTY",
        exchange="NSE",
        instrument_type="INDEX",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )

    with pytest.raises(ValidationError):
        Instrument(
            instrument_token="NIFTY26AUGFUT",
            symbol="NIFTY",
            exchange="NFO",
            instrument_type="FUTIDX",
            expiry_at=datetime(2026, 8, 27, 9, 15),
            lot_size=75,
            tick_size=Decimal("0.05"),
        )
    with pytest.raises(ValidationError):
        instrument.symbol = "BANKNIFTY"


def test_normalized_models_persist_aware_timestamps_in_utc():
    quote = Quote(
        instrument_token="NIFTY50",
        last_price=Decimal("24776.40"),
        observed_at=datetime(2026, 8, 3, 9, 45, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        source=MarketDataSource.LIVE,
    )

    assert quote.observed_at == datetime(2026, 8, 3, 4, 15, tzinfo=UTC)
    assert quote.observed_at.tzinfo is UTC


def test_broker_provenance_extension_is_backward_compatible_and_immutable():
    legacy = Instrument(
        instrument_token="legacy",
        symbol="LEGACY",
        exchange="NSE",
        instrument_type="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )
    sourced = Instrument(
        instrument_token="sourced",
        symbol="SOURCED",
        exchange="NSE",
        instrument_type="CE",
        lot_size=75,
        tick_size=Decimal("0.05"),
        source=MarketDataSource.GROWW_LIVE,
    )
    greeks = OptionGreeks(
        instrument=sourced,
        delta=Decimal("0.5"),
        source=MarketDataSource.GROWW_LIVE,
    )

    assert legacy.source is None
    assert greeks.gamma is None
    with pytest.raises(ValidationError):
        greeks.delta = Decimal("0.6")
