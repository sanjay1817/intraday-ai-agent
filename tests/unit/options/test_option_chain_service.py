"""Unit tests for `OptionChainService` (app.options.option_chain_service).

Uses a hand-written fake `OptionInstrumentSource` rather than HTTP mocking
— the seam is already abstracted via `Protocol`, so no real broker/HTTP
plumbing is needed to exercise the service's caching/error-translation
behavior.
"""

from datetime import date

import pytest

from app.domain.enums.trading import BrokerName, Exchange
from app.domain.exceptions.broker import BrokerAPIError, BrokerConnectionError
from app.options.exceptions import (
    InvalidOptionChainDataError,
    OptionChainFetchError,
    UnsupportedUnderlyingError,
)
from app.options.models import OptionInstrument, OptionType
from app.options.option_chain_service import OptionChainService


def _instrument(underlying: str, expiry: date, strike: float, option_type: OptionType) -> OptionInstrument:
    return OptionInstrument(
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        tradingsymbol=f"{underlying}{expiry.strftime('%d%b%Y').upper()}{int(strike)}{option_type.value}",
        exchange=Exchange.NFO,
        token="1",
        lot_size=50,
    )


class FakeSource:
    """A hand-written `OptionInstrumentSource` fake for tests."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.instruments: dict[str, list[OptionInstrument]] = {}
        self.exception: Exception | None = None

    async def fetch_option_instruments(self, underlying: str) -> list[OptionInstrument]:
        self.calls.append(underlying)
        if self.exception is not None:
            raise self.exception
        return self.instruments.get(underlying, [])


def make_service(source: FakeSource, *, refresh_seconds: float = 30.0) -> OptionChainService:
    return OptionChainService(
        underlyings=["NIFTY", "BANKNIFTY"], refresh_seconds=refresh_seconds, source=source
    )


async def test_get_option_chain_fetches_on_first_call() -> None:
    source = FakeSource()
    source.instruments["NIFTY"] = [_instrument("NIFTY", date(2026, 7, 30), 24000, OptionType.CE)]
    service = make_service(source)

    chain = await service.get_option_chain("NIFTY")

    assert chain.underlying == "NIFTY"
    assert len(chain.instruments) == 1
    assert source.calls == ["NIFTY"]


async def test_get_option_chain_returns_cached_chain_within_ttl() -> None:
    source = FakeSource()
    source.instruments["NIFTY"] = [_instrument("NIFTY", date(2026, 7, 30), 24000, OptionType.CE)]
    service = make_service(source, refresh_seconds=60.0)

    await service.get_option_chain("NIFTY")
    await service.get_option_chain("NIFTY")

    assert source.calls == ["NIFTY"]


async def test_get_option_chain_refetches_after_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    source = FakeSource()
    source.instruments["NIFTY"] = [_instrument("NIFTY", date(2026, 7, 30), 24000, OptionType.CE)]
    service = make_service(source, refresh_seconds=10.0)

    clock = {"t": 1000.0}
    monkeypatch.setattr("app.options.option_chain_service.time.monotonic", lambda: clock["t"])

    await service.get_option_chain("NIFTY")
    clock["t"] += 20.0  # past the 10s TTL
    await service.get_option_chain("NIFTY")

    assert source.calls == ["NIFTY", "NIFTY"]


async def test_get_option_chain_force_refresh_bypasses_fresh_cache() -> None:
    source = FakeSource()
    source.instruments["NIFTY"] = [_instrument("NIFTY", date(2026, 7, 30), 24000, OptionType.CE)]
    service = make_service(source, refresh_seconds=60.0)

    await service.get_option_chain("NIFTY")
    await service.get_option_chain("NIFTY", force_refresh=True)

    assert source.calls == ["NIFTY", "NIFTY"]


async def test_get_option_chain_is_case_insensitive_for_underlying() -> None:
    source = FakeSource()
    source.instruments["NIFTY"] = [_instrument("NIFTY", date(2026, 7, 30), 24000, OptionType.CE)]
    service = make_service(source)

    chain = await service.get_option_chain("nifty")

    assert chain.underlying == "NIFTY"


async def test_get_option_chain_raises_unsupported_underlying() -> None:
    source = FakeSource()
    service = make_service(source)

    with pytest.raises(UnsupportedUnderlyingError):
        await service.get_option_chain("SENSEX")

    assert source.calls == []


async def test_get_option_chain_wraps_broker_connection_error() -> None:
    source = FakeSource()
    source.exception = BrokerConnectionError("network down", broker=BrokerName.ANGEL_ONE)
    service = make_service(source)

    with pytest.raises(OptionChainFetchError):
        await service.get_option_chain("NIFTY")


async def test_get_option_chain_wraps_broker_api_error() -> None:
    source = FakeSource()
    source.exception = BrokerAPIError("bad request", broker=BrokerName.ANGEL_ONE, status_code=400)
    service = make_service(source)

    with pytest.raises(OptionChainFetchError):
        await service.get_option_chain("NIFTY")


async def test_get_option_chain_propagates_invalid_option_chain_data_error() -> None:
    source = FakeSource()
    source.exception = InvalidOptionChainDataError("empty", underlying="NIFTY")
    service = make_service(source)

    with pytest.raises(InvalidOptionChainDataError):
        await service.get_option_chain("NIFTY")


async def test_get_available_expiries_delegates_to_get_option_chain() -> None:
    source = FakeSource()
    source.instruments["NIFTY"] = [
        _instrument("NIFTY", date(2026, 7, 30), 24000, OptionType.CE),
        _instrument("NIFTY", date(2026, 8, 6), 24000, OptionType.CE),
    ]
    service = make_service(source)

    expiries = await service.get_available_expiries("NIFTY")

    assert expiries == (date(2026, 7, 30), date(2026, 8, 6))


async def test_get_available_strikes_delegates_to_get_option_chain() -> None:
    source = FakeSource()
    expiry = date(2026, 7, 30)
    source.instruments["NIFTY"] = [
        _instrument("NIFTY", expiry, 24000, OptionType.CE),
        _instrument("NIFTY", expiry, 24100, OptionType.PE),
    ]
    service = make_service(source)

    strikes = await service.get_available_strikes("NIFTY", expiry)

    assert strikes == (24000.0, 24100.0)


async def test_refresh_one_underlying_forces_refetch() -> None:
    source = FakeSource()
    source.instruments["NIFTY"] = [_instrument("NIFTY", date(2026, 7, 30), 24000, OptionType.CE)]
    service = make_service(source, refresh_seconds=60.0)

    await service.get_option_chain("NIFTY")
    await service.refresh("NIFTY")

    assert source.calls == ["NIFTY", "NIFTY"]


async def test_refresh_all_refreshes_every_configured_underlying() -> None:
    source = FakeSource()
    source.instruments["NIFTY"] = [_instrument("NIFTY", date(2026, 7, 30), 24000, OptionType.CE)]
    source.instruments["BANKNIFTY"] = [
        _instrument("BANKNIFTY", date(2026, 7, 30), 51000, OptionType.CE)
    ]
    service = make_service(source, refresh_seconds=60.0)

    await service.refresh()

    assert sorted(source.calls) == ["BANKNIFTY", "NIFTY"]


def test_constructor_raises_on_empty_underlyings() -> None:
    with pytest.raises(ValueError):
        OptionChainService(underlyings=[], refresh_seconds=30.0, source=FakeSource())
