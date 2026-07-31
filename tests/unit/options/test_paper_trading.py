"""Unit tests for `app.options.paper_trading`.

Uses a real `PaperTradingEngine` (already thoroughly unit-tested on its
own — see `tests/unit/paper/test_engine.py`) plus hand-written
`BrokerInterface`/`OptionInstrumentSource` fakes, mirroring
`tests/unit/options/test_recommendation.py`'s conventions.
"""

from datetime import UTC, date, datetime

import pytest

from app.domain.entities.broker import Quote
from app.domain.enums.trading import Exchange, OrderStatus
from app.options.exceptions import (
    InvalidLotQuantityError,
    OptionPositionNotFoundError,
    OptionRiskLimitExceededError,
)
from app.options.models import OptionInstrument, OptionType
from app.options.option_chain_service import OptionChainService
from app.options.paper_trading import (
    enter_option_position,
    exit_option_position,
    get_option_premium_exposure,
    get_option_trade_history,
)
from app.options.risk import OptionRiskManager
from app.options.schemas import OptionExitReason, OptionRecommendation, OptionSignal
from app.paper.engine import PaperTradingEngine

_EXPIRY = date(2026, 8, 7)
_T0 = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


class FakeBroker:
    """Minimal `BrokerInterface` double exposing only `ltp`."""

    def __init__(self, *, ltp: float = 100.0) -> None:
        self._ltp = ltp
        self.ltp_calls: list[tuple[Exchange, str]] = []

    def set_ltp(self, value: float) -> None:
        self._ltp = value

    async def ltp(self, exchange: Exchange, tradingsymbol: str) -> Quote:
        self.ltp_calls.append((exchange, tradingsymbol))
        return Quote(tradingsymbol=tradingsymbol, exchange=exchange, last_price=self._ltp)


class FakeOptionSource:
    def __init__(self) -> None:
        self.instruments: dict[str, list[OptionInstrument]] = {}

    async def fetch_option_instruments(self, underlying: str) -> list[OptionInstrument]:
        return self.instruments.get(underlying, [])


def _tradingsymbol(underlying: str, strike: float, option_type: OptionType) -> str:
    return f"{underlying}{_EXPIRY.strftime('%d%b%Y').upper()}{int(strike)}{option_type.value}"


def _instrument(
    underlying: str, strike: float, option_type: OptionType, lot_size: int | None = 50
) -> OptionInstrument:
    return OptionInstrument(
        underlying=underlying,
        expiry=_EXPIRY,
        strike=strike,
        option_type=option_type,
        tradingsymbol=_tradingsymbol(underlying, strike, option_type),
        exchange=Exchange.NFO,
        token="1",
        lot_size=lot_size,
    )


def _chain_service(instruments: list[OptionInstrument]) -> OptionChainService:
    source = FakeOptionSource()
    source.instruments["NIFTY"] = instruments
    return OptionChainService(underlyings=["NIFTY"], refresh_seconds=30.0, source=source)


def _recommendation(
    *,
    signal: OptionSignal = OptionSignal.BULLISH,
    strike: float = 24000.0,
    option_type: OptionType = OptionType.CE,
    premium: float = 100.0,
) -> OptionRecommendation:
    if signal is OptionSignal.NO_TRADE:
        return OptionRecommendation(
            underlying="NIFTY",
            signal=signal,
            underlying_ltp=24000.0,
            confidence=50.0,
            reasoning="no trade",
            generated_at=_T0,
        )
    return OptionRecommendation(
        underlying="NIFTY",
        signal=signal,
        tradingsymbol=_tradingsymbol("NIFTY", strike, option_type),
        expiry=_EXPIRY,
        strike=strike,
        option_type=option_type,
        premium=premium,
        underlying_ltp=24000.0,
        confidence=80.0,
        reasoning="strong signal",
        generated_at=_T0,
    )


def _risk_manager(**overrides: object) -> OptionRiskManager:
    kwargs: dict = {
        "max_lots_per_order": 10,
        "max_premium_per_order": 1_000_000.0,
        "max_premium_exposure": 1_000_000.0,
        "max_daily_loss": 1_000_000.0,
    }
    kwargs.update(overrides)
    return OptionRiskManager(**kwargs)  # type: ignore[arg-type]


# -- enter_option_position: happy path ------------------------------------------------------


async def test_enter_happy_path_buys_ce_with_correct_quantity_and_metadata() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager()
    recommendation = _recommendation()

    order = await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=recommendation,
        lots=2,
        default_lot_size=75,
        max_lots_per_order=10,
        stop_loss_premium=80.0,
        target_premium=130.0,
        now=_T0,
    )

    assert order.status is OrderStatus.COMPLETE
    assert order.quantity == 2 * 50  # lots * chain lot size, not the default
    assert order.symbol == recommendation.tradingsymbol
    assert order.exchange is Exchange.NFO
    assert order.average_fill_price == 100.0
    assert order.stop_loss_price == 80.0
    assert order.target_price == 130.0
    assert order.tag == "OPTION_ENTRY"
    assert order.metadata is not None
    assert order.metadata.confidence == 80.0
    assert order.metadata.reasoning == "strong signal"
    # A fresh premium is fetched, never the (possibly stale) recommendation.premium.
    assert broker.ltp_calls == [(Exchange.NFO, recommendation.tradingsymbol)]


async def test_enter_uses_default_lot_size_when_chain_lookup_misses() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([])  # no matching instrument
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager()
    recommendation = _recommendation()

    order = await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=recommendation,
        lots=2,
        default_lot_size=75,
        max_lots_per_order=10,
        now=_T0,
    )

    assert order.quantity == 2 * 75


async def test_enter_no_trade_recommendation_raises_value_error() -> None:
    broker = FakeBroker()
    chain_service = _chain_service([])
    engine = PaperTradingEngine()
    risk_manager = _risk_manager()

    with pytest.raises(ValueError, match="NO_TRADE"):
        await enter_option_position(
            broker=broker,
            engine=engine,
            option_chain_service=chain_service,
            risk_manager=risk_manager,
            recommendation=_recommendation(signal=OptionSignal.NO_TRADE),
            lots=1,
            default_lot_size=50,
            max_lots_per_order=10,
            now=_T0,
        )


async def test_enter_lots_exceeding_max_raises_invalid_lot_quantity_error() -> None:
    broker = FakeBroker()
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE)])
    engine = PaperTradingEngine()
    risk_manager = _risk_manager()

    with pytest.raises(InvalidLotQuantityError):
        await enter_option_position(
            broker=broker,
            engine=engine,
            option_chain_service=chain_service,
            risk_manager=risk_manager,
            recommendation=_recommendation(),
            lots=11,
            default_lot_size=50,
            max_lots_per_order=10,
            now=_T0,
        )


async def test_enter_zero_lots_raises_invalid_lot_quantity_error() -> None:
    broker = FakeBroker()
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE)])
    engine = PaperTradingEngine()
    risk_manager = _risk_manager()

    with pytest.raises(InvalidLotQuantityError):
        await enter_option_position(
            broker=broker,
            engine=engine,
            option_chain_service=chain_service,
            risk_manager=risk_manager,
            recommendation=_recommendation(),
            lots=0,
            default_lot_size=50,
            max_lots_per_order=10,
            now=_T0,
        )


# -- enter_option_position: risk limit breaches --------------------------------------------


async def test_enter_daily_loss_lockout_raises_risk_limit_exceeded() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager(max_daily_loss=100.0)
    risk_manager.record_trade_closed(-100.0, now=_T0)

    with pytest.raises(OptionRiskLimitExceededError) as exc_info:
        await enter_option_position(
            broker=broker,
            engine=engine,
            option_chain_service=chain_service,
            risk_manager=risk_manager,
            recommendation=_recommendation(),
            lots=1,
            default_lot_size=50,
            max_lots_per_order=10,
            now=_T0,
        )
    assert "option_max_daily_loss" in exc_info.value.reason


async def test_enter_lots_over_risk_limit_raises_risk_limit_exceeded() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager(max_lots_per_order=1)

    with pytest.raises(OptionRiskLimitExceededError) as exc_info:
        await enter_option_position(
            broker=broker,
            engine=engine,
            option_chain_service=chain_service,
            risk_manager=risk_manager,
            recommendation=_recommendation(),
            lots=2,
            default_lot_size=50,
            max_lots_per_order=10,
            now=_T0,
        )
    assert "option_max_lots_per_order" in exc_info.value.reason


async def test_enter_order_value_over_premium_limit_raises_risk_limit_exceeded() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager(max_premium_per_order=1000.0)  # order value = 1*50*100 = 5000

    with pytest.raises(OptionRiskLimitExceededError) as exc_info:
        await enter_option_position(
            broker=broker,
            engine=engine,
            option_chain_service=chain_service,
            risk_manager=risk_manager,
            recommendation=_recommendation(),
            lots=1,
            default_lot_size=50,
            max_lots_per_order=10,
            now=_T0,
        )
    assert "option_max_premium_per_order" in exc_info.value.reason


async def test_enter_exposure_over_limit_raises_risk_limit_exceeded() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service(
        [
            _instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50),
            _instrument("NIFTY", 24100.0, OptionType.CE, lot_size=50),
        ]
    )
    engine = PaperTradingEngine(initial_capital=10_000_000.0)
    risk_manager = _risk_manager(max_premium_exposure=6000.0)

    # First entry: order value = 50 * 100 = 5000, within the 6000 exposure cap.
    await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=_recommendation(strike=24000.0),
        lots=1,
        default_lot_size=50,
        max_lots_per_order=10,
        now=_T0,
    )

    # Second entry on a different contract pushes total exposure over the cap.
    with pytest.raises(OptionRiskLimitExceededError) as exc_info:
        await enter_option_position(
            broker=broker,
            engine=engine,
            option_chain_service=chain_service,
            risk_manager=risk_manager,
            recommendation=_recommendation(strike=24100.0),
            lots=1,
            default_lot_size=50,
            max_lots_per_order=10,
            now=_T0,
        )
    assert "option_max_premium_exposure" in exc_info.value.reason


async def test_enter_rejected_order_is_returned_not_raised() -> None:
    broker = FakeBroker(ltp=1_000_000.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1000.0)  # far too little cash
    risk_manager = _risk_manager(
        max_premium_per_order=1_000_000_000.0, max_premium_exposure=1_000_000_000.0
    )

    order = await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=_recommendation(),
        lots=1,
        default_lot_size=50,
        max_lots_per_order=10,
        now=_T0,
    )

    assert order.status is OrderStatus.REJECTED


# -- exit_option_position -------------------------------------------------------------------


async def test_exit_full_position_happy_path() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager()
    recommendation = _recommendation()

    entry = await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=recommendation,
        lots=1,
        default_lot_size=50,
        max_lots_per_order=10,
        now=_T0,
    )
    assert entry.status is OrderStatus.COMPLETE

    broker.set_ltp(120.0)
    exit_order = await exit_option_position(
        broker=broker,
        engine=engine,
        risk_manager=risk_manager,
        tradingsymbol=recommendation.tradingsymbol,
        reason=OptionExitReason.MANUAL,
        now=_T0,
    )

    assert exit_order.status is OrderStatus.COMPLETE
    assert exit_order.quantity == 50
    assert exit_order.tag == "MANUAL"
    position = await engine.get_position(recommendation.tradingsymbol, Exchange.NFO)
    assert position is None  # fully closed
    assert risk_manager.daily_realized_pnl == pytest.approx((120.0 - 100.0) * 50)


async def test_exit_partial_position_leaves_remainder_open() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager()
    recommendation = _recommendation()

    await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=recommendation,
        lots=2,
        default_lot_size=50,
        max_lots_per_order=10,
        now=_T0,
    )

    exit_order = await exit_option_position(
        broker=broker,
        engine=engine,
        risk_manager=risk_manager,
        tradingsymbol=recommendation.tradingsymbol,
        reason=OptionExitReason.MANUAL,
        quantity=50,
        now=_T0,
    )

    assert exit_order.quantity == 50
    position = await engine.get_position(recommendation.tradingsymbol, Exchange.NFO)
    assert position is not None
    assert position.quantity == 50


async def test_exit_no_position_raises_option_position_not_found_error() -> None:
    broker = FakeBroker()
    engine = PaperTradingEngine()
    risk_manager = _risk_manager()

    with pytest.raises(OptionPositionNotFoundError):
        await exit_option_position(
            broker=broker,
            engine=engine,
            risk_manager=risk_manager,
            tradingsymbol="NIFTY07AUG202624000CE",
            reason=OptionExitReason.MANUAL,
            now=_T0,
        )


async def test_exit_oversized_quantity_raises_value_error() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager()
    recommendation = _recommendation()

    await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=recommendation,
        lots=1,
        default_lot_size=50,
        max_lots_per_order=10,
        now=_T0,
    )

    with pytest.raises(ValueError):
        await exit_option_position(
            broker=broker,
            engine=engine,
            risk_manager=risk_manager,
            tradingsymbol=recommendation.tradingsymbol,
            reason=OptionExitReason.MANUAL,
            quantity=51,
            now=_T0,
        )


# -- get_option_trade_history -----------------------------------------------------------------


async def test_trade_history_derives_underlying_and_enriches_metadata() -> None:
    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service([_instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50)])
    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    risk_manager = _risk_manager()
    recommendation = _recommendation()

    await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=recommendation,
        lots=1,
        default_lot_size=50,
        max_lots_per_order=10,
        now=_T0,
    )
    broker.set_ltp(120.0)
    await exit_option_position(
        broker=broker,
        engine=engine,
        risk_manager=risk_manager,
        tradingsymbol=recommendation.tradingsymbol,
        reason=OptionExitReason.TARGET,
        now=_T0,
    )

    history = await get_option_trade_history(engine, underlyings=["NIFTY", "BANKNIFTY"])

    assert len(history) == 1
    entry = history[0]
    assert entry.underlying == "NIFTY"
    assert entry.tradingsymbol == recommendation.tradingsymbol
    assert entry.entry_premium == 100.0
    assert entry.exit_premium == 120.0
    assert entry.pnl == pytest.approx((120.0 - 100.0) * 50)
    assert entry.reason == "TARGET"
    assert entry.confidence == 80.0
    assert entry.holding_seconds == 0.0


# -- get_option_premium_exposure --------------------------------------------------------------


async def test_get_option_premium_exposure_sums_open_nfo_positions_only() -> None:
    from app.domain.enums.trading import OrderSide
    from app.paper.dto import PaperOrderRequest, PaperOrderType

    broker = FakeBroker(ltp=100.0)
    chain_service = _chain_service(
        [
            _instrument("NIFTY", 24000.0, OptionType.CE, lot_size=50),
            _instrument("NIFTY", 24100.0, OptionType.CE, lot_size=50),
        ]
    )
    engine = PaperTradingEngine(initial_capital=10_000_000.0)
    risk_manager = _risk_manager()

    assert await get_option_premium_exposure(engine) == 0.0

    await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=_recommendation(strike=24000.0),
        lots=1,
        default_lot_size=50,
        max_lots_per_order=10,
        now=_T0,
    )
    await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=chain_service,
        risk_manager=risk_manager,
        recommendation=_recommendation(strike=24100.0),
        lots=1,
        default_lot_size=50,
        max_lots_per_order=10,
        now=_T0,
    )

    # 2 positions * 50 qty * 100 premium each = 10000
    assert await get_option_premium_exposure(engine) == pytest.approx(10_000.0)

    # An equity (NSE) position must not contribute.
    await engine.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            order_type=PaperOrderType.MARKET,
            quantity=10,
        ),
        current_price=1500.0,
        now=_T0,
    )
    assert await get_option_premium_exposure(engine) == pytest.approx(10_000.0)


async def test_trade_history_excludes_non_nfo_trades() -> None:
    from app.domain.enums.trading import OrderSide
    from app.paper.dto import PaperOrderRequest, PaperOrderType

    engine = PaperTradingEngine(initial_capital=1_000_000.0)
    # Place and close a plain equity (NSE) trade.
    await engine.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            order_type=PaperOrderType.MARKET,
            quantity=10,
        ),
        current_price=1500.0,
        now=_T0,
    )
    await engine.place_order(
        PaperOrderRequest(
            symbol="INFY-EQ",
            exchange=Exchange.NSE,
            side=OrderSide.SELL,
            order_type=PaperOrderType.MARKET,
            quantity=10,
        ),
        current_price=1600.0,
        now=_T0,
    )

    history = await get_option_trade_history(engine, underlyings=["NIFTY"])

    assert history == []
