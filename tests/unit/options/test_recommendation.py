"""Unit tests for `app.options.recommendation.generate_option_recommendation`.

Uses hand-written fakes for `BrokerInterface` (candles + LTP) and
`OptionInstrumentSource` (feeding a real `OptionChainService`) rather than
HTTP mocking — mirrors `tests/unit/options/test_option_chain_service.py`
and `tests/unit/api/v1/routers/test_signals.py`'s fake-broker conventions.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.entities.broker import HistoricalBar, Quote
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.broker import BrokerAPIError, BrokerConnectionError
from app.domain.exceptions.market import NoHistoricalDataError
from app.options.exceptions import OptionChainFetchError, PremiumUnavailableError
from app.options.models import ExpiryMode, OptionInstrument, OptionType, StrikeMode
from app.options.option_chain_service import OptionChainService
from app.options.recommendation import generate_option_recommendation
from app.options.schemas import OptionSignal


def _bars(closes: list[float]) -> list[HistoricalBar]:
    start = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)
    bars = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i > 0 else close
        high = max(open_, close) * 1.01
        low = min(open_, close) * 0.99
        bars.append(
            HistoricalBar(
                timestamp=start + timedelta(minutes=5 * i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000 + i,
            )
        )
    return bars


def _uptrend_closes(n: int = 60) -> list[float]:
    return [100 + i * 2.0 for i in range(n)]


def _downtrend_closes(n: int = 60) -> list[float]:
    return [100 + (n - i) * 2.0 for i in range(n)]


def _flat_closes(n: int = 60) -> list[float]:
    return [100.0 for _ in range(n)]


class FakeBroker:
    """A minimal hand-written `BrokerInterface` double exposing only the
    two methods `generate_option_recommendation` calls."""

    def __init__(self, *, bars: list[HistoricalBar], ltp: float | None = None,
                 ltp_exception: Exception | None = None) -> None:
        self._bars = bars
        self._ltp = ltp
        self._ltp_exception = ltp_exception
        self.ltp_calls: list[tuple[Exchange, str]] = []
        self.historical_calls: list[tuple[Exchange, str, HistoricalInterval]] = []

    async def historical_data(self, exchange, tradingsymbol, interval, from_date, to_date):
        self.historical_calls.append((exchange, tradingsymbol, interval))
        return self._bars

    async def ltp(self, exchange, tradingsymbol):
        self.ltp_calls.append((exchange, tradingsymbol))
        if self._ltp_exception is not None:
            raise self._ltp_exception
        return Quote(tradingsymbol=tradingsymbol, exchange=exchange, last_price=self._ltp or 0.0)


class FakeOptionSource:
    """A hand-written `OptionInstrumentSource` fake, mirroring
    `test_option_chain_service.py`'s `FakeSource`."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.instruments: dict[str, list[OptionInstrument]] = {}
        self.exception: Exception | None = None

    async def fetch_option_instruments(self, underlying: str) -> list[OptionInstrument]:
        self.calls.append(underlying)
        if self.exception is not None:
            raise self.exception
        return self.instruments.get(underlying, [])


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


def _make_chain_service(source: FakeOptionSource, underlyings: list[str] | None = None) -> OptionChainService:
    return OptionChainService(
        underlyings=underlyings or ["NIFTY", "BANKNIFTY"],
        refresh_seconds=30.0,
        source=source,
    )


_NEAR_EXPIRY = date(2026, 8, 7)
_FAR_EXPIRY = date(2026, 8, 14)


def _seed_nifty_chain(source: FakeOptionSource, strikes: list[float]) -> None:
    instruments = []
    for expiry in (_NEAR_EXPIRY, _FAR_EXPIRY):
        for strike in strikes:
            instruments.append(_instrument("NIFTY", expiry, strike, OptionType.CE))
            instruments.append(_instrument("NIFTY", expiry, strike, OptionType.PE))
    source.instruments["NIFTY"] = instruments


async def test_buy_maps_to_bullish_ce_with_atm_strike_and_expiry() -> None:
    bars = _bars(_uptrend_closes())
    underlying_ltp = bars[-1].close
    broker = FakeBroker(bars=bars, ltp=42.5)
    source = FakeOptionSource()
    strikes = [round(underlying_ltp - 200 + 100 * i, -2) or 100.0 for i in range(6)]
    strikes = sorted({max(s, 100.0) for s in strikes})
    _seed_nifty_chain(source, strikes)
    service = _make_chain_service(source)

    result = await generate_option_recommendation(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        option_chain_service=service,
        underlying="NIFTY",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        strike_mode=StrikeMode.ATM,
        expiry_mode=ExpiryMode.NEAREST_WEEKLY,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert result.signal == OptionSignal.BULLISH
    assert result.option_type == OptionType.CE
    assert result.expiry == _NEAR_EXPIRY
    assert result.premium == 42.5
    assert result.underlying_ltp == underlying_ltp
    assert result.tradingsymbol is not None
    assert result.tradingsymbol.endswith("CE")
    assert result.strike is not None
    assert broker.ltp_calls == [(Exchange.NFO, result.tradingsymbol)]


async def test_sell_maps_to_bearish_pe() -> None:
    bars = _bars(_downtrend_closes())
    underlying_ltp = bars[-1].close
    broker = FakeBroker(bars=bars, ltp=55.0)
    source = FakeOptionSource()
    strikes = sorted({max(round(underlying_ltp - 200 + 100 * i, -2), 100.0) for i in range(6)})
    _seed_nifty_chain(source, strikes)
    service = _make_chain_service(source)

    result = await generate_option_recommendation(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        option_chain_service=service,
        underlying="NIFTY",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        strike_mode=StrikeMode.ATM,
        expiry_mode=ExpiryMode.NEAREST_WEEKLY,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert result.signal == OptionSignal.BEARISH
    assert result.option_type == OptionType.PE
    assert result.tradingsymbol is not None
    assert result.tradingsymbol.endswith("PE")


async def test_hold_returns_no_trade_and_never_touches_option_chain() -> None:
    bars = _bars(_flat_closes())
    broker = FakeBroker(bars=bars, ltp=99.0)
    source = FakeOptionSource()
    service = _make_chain_service(source)

    result = await generate_option_recommendation(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        option_chain_service=service,
        underlying="NIFTY",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        strike_mode=StrikeMode.ATM,
        expiry_mode=ExpiryMode.NEAREST_WEEKLY,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert result.signal == OptionSignal.NO_TRADE
    assert result.tradingsymbol is None
    assert result.expiry is None
    assert result.strike is None
    assert result.option_type is None
    assert result.premium is None
    assert source.calls == []
    assert broker.ltp_calls == []


async def test_expiry_mode_is_passed_through_to_expiry_selector() -> None:
    bars = _bars(_uptrend_closes())
    underlying_ltp = bars[-1].close
    broker = FakeBroker(bars=bars, ltp=42.5)
    source = FakeOptionSource()
    strikes = sorted({max(round(underlying_ltp - 200 + 100 * i, -2), 100.0) for i in range(6)})
    _seed_nifty_chain(source, strikes)
    service = _make_chain_service(source)

    result = await generate_option_recommendation(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        option_chain_service=service,
        underlying="NIFTY",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        strike_mode=StrikeMode.ATM,
        expiry_mode=ExpiryMode.MONTHLY,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert result.expiry == _FAR_EXPIRY


async def test_strike_mode_is_passed_through_to_strike_selector() -> None:
    bars = _bars(_uptrend_closes())
    underlying_ltp = bars[-1].close
    broker = FakeBroker(bars=bars, ltp=42.5)
    source = FakeOptionSource()
    strikes = sorted({max(round(underlying_ltp - 300 + 100 * i, -2), 100.0) for i in range(8)})
    _seed_nifty_chain(source, strikes)
    service = _make_chain_service(source)

    atm_result = await generate_option_recommendation(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        option_chain_service=service,
        underlying="NIFTY",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        strike_mode=StrikeMode.ATM,
        expiry_mode=ExpiryMode.NEAREST_WEEKLY,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )
    otm_result = await generate_option_recommendation(
        broker=broker,
        broker_name=BrokerName.ANGEL_ONE,
        option_chain_service=service,
        underlying="NIFTY",
        timeframe=HistoricalInterval.FIVE_MINUTE,
        strike_mode=StrikeMode.OTM,
        expiry_mode=ExpiryMode.NEAREST_WEEKLY,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert atm_result.strike != otm_result.strike


async def test_premium_unavailable_error_raised_when_broker_ltp_fails() -> None:
    bars = _bars(_uptrend_closes())
    underlying_ltp = bars[-1].close
    broker = FakeBroker(
        bars=bars,
        ltp_exception=BrokerAPIError("bad symbol", broker=BrokerName.ANGEL_ONE, status_code=400),
    )
    source = FakeOptionSource()
    strikes = sorted({max(round(underlying_ltp - 200 + 100 * i, -2), 100.0) for i in range(6)})
    _seed_nifty_chain(source, strikes)
    service = _make_chain_service(source)

    with pytest.raises(PremiumUnavailableError):
        await generate_option_recommendation(
            broker=broker,
            broker_name=BrokerName.ANGEL_ONE,
            option_chain_service=service,
            underlying="NIFTY",
            timeframe=HistoricalInterval.FIVE_MINUTE,
            strike_mode=StrikeMode.ATM,
            expiry_mode=ExpiryMode.NEAREST_WEEKLY,
            now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        )


async def test_broker_connection_error_on_ltp_is_wrapped_too() -> None:
    bars = _bars(_uptrend_closes())
    underlying_ltp = bars[-1].close
    broker = FakeBroker(
        bars=bars,
        ltp_exception=BrokerConnectionError("network down", broker=BrokerName.ANGEL_ONE),
    )
    source = FakeOptionSource()
    strikes = sorted({max(round(underlying_ltp - 200 + 100 * i, -2), 100.0) for i in range(6)})
    _seed_nifty_chain(source, strikes)
    service = _make_chain_service(source)

    with pytest.raises(PremiumUnavailableError):
        await generate_option_recommendation(
            broker=broker,
            broker_name=BrokerName.ANGEL_ONE,
            option_chain_service=service,
            underlying="NIFTY",
            timeframe=HistoricalInterval.FIVE_MINUTE,
            strike_mode=StrikeMode.ATM,
            expiry_mode=ExpiryMode.NEAREST_WEEKLY,
            now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        )


async def test_no_historical_data_error_propagates_unchanged() -> None:
    broker = FakeBroker(bars=[])
    source = FakeOptionSource()
    service = _make_chain_service(source)

    with pytest.raises(NoHistoricalDataError):
        await generate_option_recommendation(
            broker=broker,
            broker_name=BrokerName.ANGEL_ONE,
            option_chain_service=service,
            underlying="NIFTY",
            timeframe=HistoricalInterval.FIVE_MINUTE,
            strike_mode=StrikeMode.ATM,
            expiry_mode=ExpiryMode.NEAREST_WEEKLY,
        )


async def test_option_chain_fetch_error_propagates_for_buy_sell() -> None:
    bars = _bars(_uptrend_closes())
    broker = FakeBroker(bars=bars, ltp=42.5)
    source = FakeOptionSource()
    source.exception = BrokerConnectionError("chain down", broker=BrokerName.ANGEL_ONE)
    service = _make_chain_service(source)

    with pytest.raises(OptionChainFetchError):
        await generate_option_recommendation(
            broker=broker,
            broker_name=BrokerName.ANGEL_ONE,
            option_chain_service=service,
            underlying="NIFTY",
            timeframe=HistoricalInterval.FIVE_MINUTE,
            strike_mode=StrikeMode.ATM,
            expiry_mode=ExpiryMode.NEAREST_WEEKLY,
            now=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        )
