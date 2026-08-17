from __future__ import annotations

import math
import os
import stat
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

import pyotp

from .config import Settings
from .models import (
    AccountSummary,
    DataSource,
    Instrument,
    NiftySnapshot,
    NiftyUniverse,
    OptionSnapshot,
    OptionWindow,
    Position,
    Quote,
    ReconciledFill,
    ReconciledOrder,
    SafeError,
)

SUPPORTED_GROWW_VERSION = "1.5.0"
ORDER_SEGMENTS = ("CASH", "FNO", "COMMODITY")


class GrowwError(RuntimeError):
    def __init__(self, code: str, reason: str, subsystem: str = "groww") -> None:
        self.detail = SafeError(
            code=code,
            reason=reason,
            subsystem=subsystem,
            correlation_id=uuid4().hex,
        )
        super().__init__(f"{code} [{self.detail.correlation_id}]: {reason}")


def _fail(code: str, reason: str, subsystem: str = "groww") -> GrowwError:
    return GrowwError(code, reason, subsystem)


def _require_supported_version() -> None:
    try:
        installed = metadata.version("growwapi")
    except metadata.PackageNotFoundError:
        installed = None
    if installed != SUPPORTED_GROWW_VERSION:
        raise _fail("GROWW_SDK_VERSION_UNSUPPORTED", "Groww SDK version is not reviewed")


def _load_sdk() -> tuple[type[Any], type[Any]]:
    _require_supported_version()
    from growwapi import GrowwAPI, GrowwFeed

    return GrowwAPI, GrowwFeed


@contextmanager
def _safe_errors(subsystem: str = "groww") -> Iterator[None]:
    try:
        yield
    except GrowwError:
        raise
    except Exception as exc:
        name = type(exc).__name__
        if subsystem == "groww_auth" and name.startswith("GrowwAPI"):
            raise _fail("GROWW_AUTH_FAILED", "Groww authentication failed", subsystem) from None
        if name == "GrowwAPIAuthenticationException":
            raise _fail("GROWW_AUTH_FAILED", "Groww authentication failed", subsystem) from None
        if name == "GrowwAPIAuthorisationException":
            raise _fail("GROWW_PERMISSION_DENIED", "Groww permission or entitlement denied", subsystem) from None
        if name == "GrowwAPIRateLimitException":
            raise _fail("GROWW_RATE_LIMITED", "Groww rate limit reached", subsystem) from None
        if name in {"GrowwAPITimeoutException", "Timeout", "ReadTimeout", "ConnectTimeout"}:
            raise _fail("GROWW_TIMEOUT", "Groww request timed out", subsystem) from None
        if isinstance(exc, (KeyError, TypeError, ValueError, ArithmeticError)):
            raise _fail("GROWW_CONTRACT_MISMATCH", "Groww returned an unexpected response shape", subsystem) from None
        raise _fail("GROWW_UNAVAILABLE", "Groww request failed", subsystem) from None


def _required(mapping: dict[str, Any], key: str) -> Any:
    value = mapping[key]
    if value is None or value == "":
        raise ValueError(key)
    return value


def _optional(value: Any) -> Any | None:
    if value is None or value == "" or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    value = _optional(value)
    return None if value is None else _decimal(value)


def _datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp")
    return parsed.astimezone(timezone.utc)


def _quote_time(payload: dict[str, Any], fallback: datetime | None = None) -> datetime:
    milliseconds = _optional(payload.get("last_trade_time"))
    if milliseconds is None:
        if fallback is None:
            raise ValueError("last_trade_time")
        return fallback.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(milliseconds) / 1000, timezone.utc)


def _instrument(row: dict[str, Any]) -> Instrument:
    kind = str(_required(row, "instrument_type"))
    option_type = kind if kind in {"CE", "PE"} else None
    expiry = _optional(row.get("expiry_date"))
    return Instrument(
        trading_symbol=str(_required(row, "trading_symbol")),
        exchange_token=str(_required(row, "exchange_token")),
        exchange=str(_required(row, "exchange")),
        segment=str(_required(row, "segment")),
        instrument_type=kind,
        underlying_symbol=_optional(row.get("underlying_symbol")),
        expiry=date.fromisoformat(str(expiry)) if expiry is not None else None,
        strike=_optional_decimal(row.get("strike_price")),
        option_type=option_type,
        lot_size=int(value) if (value := _optional(row.get("lot_size"))) is not None else None,
        tick_size=_optional_decimal(row.get("tick_size")),
    )


class GrowwReadClient:
    def __init__(self, sdk_client: Any, timeout_seconds: int = 5) -> None:
        _require_supported_version()
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        self._client = sdk_client
        self._timeout_seconds = timeout_seconds
        self._audit_raw: dict[str, Any] = {}

    @property
    def audit_raw(self) -> dict[str, Any]:
        return deepcopy(self._audit_raw)

    @classmethod
    def authenticate(cls, settings: Settings) -> GrowwReadClient:
        if not settings.groww_configured:
            raise _fail("GROWW_CONFIG_MISSING", "Groww TOTP credentials are not configured")
        token_secret = settings.groww_totp_token
        totp_secret = settings.groww_totp_secret
        assert token_secret is not None and totp_secret is not None
        token = token_secret.get_secret_value()
        secret = totp_secret.get_secret_value()
        GrowwAPI, _ = _load_sdk()
        with _safe_errors("groww_auth"):
            access_token = GrowwAPI.get_access_token(
                api_key=token,
                totp=pyotp.TOTP(secret).now(),
            )
            return cls(GrowwAPI(access_token), settings.groww_request_timeout_seconds)

    def instruments(self) -> tuple[Instrument, ...]:
        with _safe_errors():
            frame = self._client.get_all_instruments()
            rows = frame.to_dict(orient="records")
            if not isinstance(rows, list):
                raise ValueError("instruments")
            self._audit_raw["instruments"] = {"rows": rows}
            return tuple(_instrument(row) for row in rows)

    def spot_quote(self, instrument: Instrument, observed_at: datetime | None = None) -> Quote:
        observed_at = observed_at or datetime.now(timezone.utc)
        with _safe_errors():
            payload = self._client.get_quote(
                trading_symbol=instrument.trading_symbol,
                exchange=instrument.exchange,
                segment=instrument.segment,
                timeout=self._timeout_seconds,
            )
            if not isinstance(payload, dict):
                raise ValueError("spot quote")
            self._audit_raw["spot_bootstrap"] = {"spot_quote": payload}
            return Quote(
                instrument=instrument,
                price=_decimal(_required(payload, "last_price")),
                source=DataSource.GROWW_LIVE,
                source_at=_quote_time(payload, observed_at),
            )

    def market_snapshot(
        self,
        universe: NiftyUniverse,
        observed_at: datetime | None = None,
    ) -> tuple[NiftySnapshot, OptionWindow]:
        observed_at = observed_at or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise _fail("GROWW_CONTRACT_MISMATCH", "Observation timestamp must be timezone-aware")
        with _safe_errors():
            spot_payload = self._client.get_quote(
                trading_symbol=universe.spot.trading_symbol,
                exchange=universe.spot.exchange,
                segment=universe.spot.segment,
                timeout=self._timeout_seconds,
            )
            future_payload = self._client.get_quote(
                trading_symbol=universe.future.trading_symbol,
                exchange=universe.future.exchange,
                segment=universe.future.segment,
                timeout=self._timeout_seconds,
            )
            chain = self._client.get_option_chain(
                exchange=universe.spot.exchange,
                underlying=universe.spot.trading_symbol,
                expiry_date=universe.option_expiry.isoformat(),
                timeout=self._timeout_seconds,
            )
            if not all(isinstance(item, dict) for item in (spot_payload, future_payload, chain)):
                raise ValueError("market payload")
            self._audit_raw["market_snapshot"] = {
                "spot_quote": spot_payload,
                "future_quote": future_payload,
                "option_chain": chain,
            }

            spot = Quote(
                instrument=universe.spot,
                price=_decimal(_required(spot_payload, "last_price")),
                source=DataSource.GROWW_LIVE,
                source_at=_quote_time(spot_payload, observed_at),
            )
            future = Quote(
                instrument=universe.future,
                price=_decimal(_required(future_payload, "last_price")),
                source=DataSource.GROWW_LIVE,
                source_at=_quote_time(future_payload),
            )

            strikes = _required(chain, "strikes")
            if not isinstance(strikes, dict):
                raise ValueError("strikes")
            known = {item.trading_symbol: item for item in universe.options}
            contracts: list[OptionSnapshot] = []
            found: set[str] = set()
            for sides in strikes.values():
                if not isinstance(sides, dict):
                    raise ValueError("strike")
                for side in ("CE", "PE"):
                    payload = sides.get(side)
                    if payload is None:
                        continue
                    if not isinstance(payload, dict):
                        raise ValueError("contract")
                    symbol = str(_required(payload, "trading_symbol"))
                    if symbol not in known:
                        continue
                    if symbol in found:
                        raise ValueError("duplicate option contract")
                    found.add(symbol)
                    greeks = payload.get("greeks") or {}
                    if not isinstance(greeks, dict):
                        raise ValueError("greeks")
                    contracts.append(
                        OptionSnapshot(
                            instrument=known[symbol],
                            ltp=_optional_decimal(payload.get("ltp")),
                            volume=_optional_decimal(payload.get("volume")),
                            open_interest=_optional_decimal(payload.get("open_interest")),
                            implied_volatility=_optional_decimal(greeks.get("iv")),
                            delta=_optional_decimal(greeks.get("delta")),
                            gamma=_optional_decimal(greeks.get("gamma")),
                            theta=_optional_decimal(greeks.get("theta")),
                            vega=_optional_decimal(greeks.get("vega")),
                            source=DataSource.GROWW_LIVE,
                            source_at=observed_at,
                        )
                    )
            if found != set(known):
                raise ValueError("required option contracts are missing")
            return (
                NiftySnapshot(spot=spot, future=future),
                OptionWindow(
                    expiry=universe.option_expiry,
                    atm_strike=universe.atm_strike,
                    contracts=tuple(contracts),
                    source_at=observed_at,
                ),
            )

    def account_snapshot(self, observed_at: datetime | None = None) -> AccountSummary:
        observed_at = observed_at or datetime.now(timezone.utc)
        with _safe_errors():
            margin = self._client.get_available_margin_details(timeout=self._timeout_seconds)
            positions_payload = self._client.get_positions_for_user(timeout=self._timeout_seconds)
            if not isinstance(margin, dict) or not isinstance(positions_payload, dict):
                raise ValueError("account payload")
            self._audit_raw["account_snapshot"] = {
                "margin": margin,
                "positions": positions_payload,
            }
            rows = _required(positions_payload, "positions")
            if not isinstance(rows, list):
                raise ValueError("positions")
            positions = tuple(
                Position(
                    trading_symbol=str(_required(row, "trading_symbol")),
                    quantity=_decimal(_required(row, "quantity")),
                    average_price=_optional_decimal(row.get("net_price")),
                    pnl=_optional_decimal(row.get("realised_pnl")),
                )
                for row in rows
            )
            return AccountSummary(
                available_margin=_optional_decimal(margin.get("clear_cash")),
                used_margin=_optional_decimal(margin.get("net_margin_used")),
                positions=positions,
                source_at=observed_at,
            )

    def orders_and_fills(self) -> tuple[ReconciledOrder, ...]:
        with _safe_errors():
            audit = {"order_pages": [], "fill_pages": []}
            self._audit_raw["orders_and_fills"] = audit
            orders: dict[str, tuple[str, dict[str, Any]]] = {}
            for segment in ORDER_SEGMENTS:
                page = 0
                while True:
                    payload = self._client.get_order_list(
                        page=page, page_size=25, segment=segment, timeout=self._timeout_seconds
                    )
                    audit["order_pages"].append({"segment": segment, "page": page, "payload": payload})
                    rows = _required(payload, "order_list")
                    if not isinstance(rows, list) or len(rows) > 25:
                        raise ValueError("order_list")
                    before = len(orders)
                    for row in rows:
                        order_id = str(_required(row, "groww_order_id"))
                        if str(_required(row, "segment")) != segment:
                            raise ValueError("order segment")
                        orders.setdefault(order_id, (segment, row))
                    if len(rows) == 25 and len(orders) == before:
                        raise _fail(
                            "GROWW_CONTRACT_MISMATCH",
                            "Groww repeated a full order page",
                        )
                    if len(rows) < 25:
                        break
                    page += 1

            reconciled: list[ReconciledOrder] = []
            for order_id, (segment, row) in orders.items():
                fills: dict[str, ReconciledFill] = {}
                page = 0
                while True:
                    payload = self._client.get_trade_list_for_order(
                        groww_order_id=order_id,
                        segment=segment,
                        page=page,
                        page_size=50,
                        timeout=self._timeout_seconds,
                    )
                    audit["fill_pages"].append(
                        {"order_id": order_id, "segment": segment, "page": page, "payload": payload}
                    )
                    trades = _required(payload, "trade_list")
                    if not isinstance(trades, list) or len(trades) > 50:
                        raise ValueError("trade_list")
                    before = len(fills)
                    for trade in trades:
                        trade_id = str(
                            _optional(trade.get("groww_trade_id"))
                            or _required(trade, "exchange_trade_id")
                        )
                        fills.setdefault(
                            trade_id,
                            ReconciledFill(
                                trade_id=trade_id,
                                order_id=order_id,
                                segment=segment,
                                quantity=_decimal(_required(trade, "quantity")),
                                price=_decimal(_required(trade, "price")),
                                source_at=_datetime(_required(trade, "trade_date_time")),
                            ),
                        )
                    if len(trades) == 50 and len(fills) == before:
                        raise _fail(
                            "GROWW_CONTRACT_MISMATCH",
                            "Groww repeated a full fill page",
                        )
                    if len(trades) < 50:
                        break
                    page += 1
                reconciled.append(
                    ReconciledOrder(
                        order_id=order_id,
                        segment=segment,
                        trading_symbol=str(_required(row, "trading_symbol")),
                        status=str(_required(row, "order_status")),
                        quantity=_decimal(_required(row, "quantity")),
                        price=_optional_decimal(row.get("price")),
                        source_at=_datetime(_required(row, "created_at")),
                        fills=tuple(fills.values()),
                    )
                )
            return tuple(reconciled)

    @contextmanager
    def open_feed(self, credential_dir: str | Path) -> Iterator[GrowwFeedClient]:
        directory = Path(credential_dir)
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                directory.chmod(0o700)
                directory_stat = directory.stat()
                if stat.S_IMODE(directory_stat.st_mode) & 0o077 or directory_stat.st_uid != os.getuid():
                    raise _fail(
                        "GROWW_FEED_DIR_UNSAFE",
                        "Groww feed credential directory is not private",
                    )
        except GrowwError:
            raise
        except OSError:
            raise _fail(
                "GROWW_FEED_DIR_UNSAFE",
                "Groww feed credential directory is unavailable",
            ) from None

        _, GrowwFeed = _load_sdk()
        import growwapi.common.files as files

        original_get_cwd = files.get_cwd
        previous_umask: int | None = None
        files.get_cwd = lambda file=__file__: str(directory)
        try:
            if os.name != "nt":
                previous_umask = os.umask(0o077)
            with _safe_errors("groww_feed"):
                feed = GrowwFeed(self._client)
            if previous_umask is not None:
                os.umask(previous_umask)
                previous_umask = None
            yield GrowwFeedClient(feed)
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)
            files.get_cwd = original_get_cwd
            for name in ("a.creds", "b.creds"):
                try:
                    (directory / name).unlink()
                except FileNotFoundError:
                    pass


class GrowwFeedClient:
    def __init__(self, feed: Any) -> None:
        _require_supported_version()
        self._feed = feed

    @staticmethod
    def _items(instruments: list[Instrument] | tuple[Instrument, ...]) -> list[dict[str, str]]:
        return [
            {
                "exchange": item.exchange,
                "segment": item.segment,
                "exchange_token": item.exchange_token,
            }
            for item in instruments
        ]

    def subscribe_index_value(
        self,
        instruments: list[Instrument] | tuple[Instrument, ...],
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        with _safe_errors("groww_feed"):
            return self._feed.subscribe_index_value(
                self._items(instruments), on_data_received=callback
            )

    def get_index_value(self) -> dict[str, Any]:
        with _safe_errors("groww_feed"):
            payload = self._feed.get_index_value()
            if not isinstance(payload, dict):
                raise ValueError("index feed")
            return payload

    def unsubscribe_index_value(self, instruments: list[Instrument] | tuple[Instrument, ...]) -> Any:
        with _safe_errors("groww_feed"):
            return self._feed.unsubscribe_index_value(self._items(instruments))

    def subscribe_ltp(
        self,
        instruments: list[Instrument] | tuple[Instrument, ...],
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        with _safe_errors("groww_feed"):
            return self._feed.subscribe_ltp(self._items(instruments), on_data_received=callback)

    def get_ltp(self) -> dict[str, Any]:
        with _safe_errors("groww_feed"):
            payload = self._feed.get_ltp()
            if not isinstance(payload, dict):
                raise ValueError("ltp feed")
            return payload

    def unsubscribe_ltp(self, instruments: list[Instrument] | tuple[Instrument, ...]) -> Any:
        with _safe_errors("groww_feed"):
            return self._feed.unsubscribe_ltp(self._items(instruments))

    def subscribe_market_depth(
        self,
        instruments: list[Instrument] | tuple[Instrument, ...],
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        with _safe_errors("groww_feed"):
            return self._feed.subscribe_market_depth(
                self._items(instruments), on_data_received=callback
            )

    def get_market_depth(self) -> dict[str, Any]:
        with _safe_errors("groww_feed"):
            payload = self._feed.get_market_depth()
            if not isinstance(payload, dict):
                raise ValueError("market depth feed")
            return payload

    def unsubscribe_market_depth(self, instruments: list[Instrument] | tuple[Instrument, ...]) -> Any:
        with _safe_errors("groww_feed"):
            return self._feed.unsubscribe_market_depth(self._items(instruments))

    def consume(self) -> None:
        with _safe_errors("groww_feed"):
            self._feed.consume()
