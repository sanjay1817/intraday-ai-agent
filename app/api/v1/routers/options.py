"""The Options Recommendation API: the on-demand HTTP endpoint that
recommends (never places) one option contract for a configured
underlying, bridging the existing AI Signal Engine (see
`app.api.v1.routers.signals`) to Phase 1's option-chain infrastructure
(`app.options`) via `app.options.recommendation`.

Mirrors `signals.py`'s shape deliberately: a pure, synchronous-per-request
pipeline with no background process and no persisted state. `get_broker`
is imported from `signals.py` rather than redefined here — one dependency
provider, reused by every router that needs the shared broker adapter.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.routers.paper import get_paper_engine
from app.api.v1.routers.signals import get_broker
from app.brokers.base import BrokerInterface
from app.config.settings import Settings, get_settings
from app.domain.enums.trading import HistoricalInterval
from app.options.auto_trading import AutoOptionsOrchestrator, AutoOptionsStatus
from app.options.exceptions import OptionsInfrastructureUnavailableError, TradingModeNotEnabledError
from app.options.models import ExpiryMode, StrikeMode
from app.options.option_chain_service import OptionChainService
from app.options.paper_trading import (
    enter_option_position,
    exit_option_position,
    get_option_premium_exposure,
    get_option_trade_history,
)
from app.options.recommendation import generate_option_recommendation
from app.options.risk import OptionRiskManager
from app.options.schemas import (
    OptionExitReason,
    OptionRecommendation,
    OptionRiskStatus,
    OptionSignal,
    OptionTradeHistoryEntry,
)
from app.options.underlying_resolver import UnderlyingResolver
from app.paper.engine import PaperTradingEngine
from app.paper.models import PaperOrder

router = APIRouter(prefix="/api/v1/options", tags=["options"])


def get_option_chain_service_or_error(request: Request) -> OptionChainService:
    """The shared `OptionChainService` constructed once at application
    startup (see `app.main`'s lifespan) — read directly off
    `request.app.state` (same pattern as `get_broker` above and
    `app.options.option_chain_service.get_option_chain_service`, which
    this duplicates rather than reuses only because that function has no
    error-raising behavior of its own to delegate to).

    Raises:
        OptionsInfrastructureUnavailableError: `app.state.option_chain_service`
            is `None` — the configured broker isn't Angel One (Phase 1's
            only `OptionInstrumentSource` implementation so far).
    """

    service = request.app.state.option_chain_service
    if service is None:
        raise OptionsInfrastructureUnavailableError(
            "no OptionChainService configured — the resolved broker isn't Angel One"
        )
    return service  # type: ignore[no-any-return]


def get_option_risk_manager_or_error(request: Request) -> OptionRiskManager:
    """The shared `OptionRiskManager` constructed once at application
    startup (see `app.main`'s lifespan) alongside `option_chain_service`,
    under the identical condition — `None` for the same reason.

    Raises:
        OptionsInfrastructureUnavailableError: `app.state.option_risk_manager`
            is `None` — reuses this Phase 2 exception rather than
            inventing a new one for the same underlying condition (the
            resolved broker isn't Angel One).
    """

    manager = request.app.state.option_risk_manager
    if manager is None:
        raise OptionsInfrastructureUnavailableError(
            "no OptionRiskManager configured — the resolved broker isn't Angel One"
        )
    return manager  # type: ignore[no-any-return]


def get_underlying_resolver_or_error(request: Request) -> UnderlyingResolver:
    """The shared `UnderlyingResolver` constructed once at application
    startup (see `app.main`'s lifespan) alongside `option_chain_service`,
    under the identical condition — `None` for the same reason.

    Raises:
        OptionsInfrastructureUnavailableError: `app.state.underlying_resolver`
            is `None` — reuses this Phase 2 exception rather than
            inventing a new one for the same underlying condition (the
            resolved broker isn't Angel One).
    """

    resolver = request.app.state.underlying_resolver
    if resolver is None:
        raise OptionsInfrastructureUnavailableError(
            "no UnderlyingResolver configured — the resolved broker isn't Angel One"
        )
    return resolver  # type: ignore[no-any-return]


def get_auto_options_orchestrator_or_error(request: Request) -> AutoOptionsOrchestrator:
    """The shared `AutoOptionsOrchestrator` constructed once at
    application startup (see `app.main`'s lifespan) alongside
    `option_chain_service`/`option_risk_manager`, under the identical
    condition — `None` for the same reason.

    Raises:
        OptionsInfrastructureUnavailableError: `app.state.auto_options_orchestrator`
            is `None` — reuses this Phase 2 exception rather than
            inventing a new one for the same underlying condition (the
            resolved broker isn't Angel One).
    """

    orchestrator = request.app.state.auto_options_orchestrator
    if orchestrator is None:
        raise OptionsInfrastructureUnavailableError(
            "no AutoOptionsOrchestrator configured — the resolved broker isn't Angel One"
        )
    return orchestrator  # type: ignore[no-any-return]


@router.get("/recommendation")
async def get_option_recommendation(
    broker: Annotated[BrokerInterface, Depends(get_broker)],
    option_chain_service: Annotated[OptionChainService, Depends(get_option_chain_service_or_error)],
    underlying_resolver: Annotated[UnderlyingResolver, Depends(get_underlying_resolver_or_error)],
    settings: Annotated[Settings, Depends(get_settings)],
    underlying: Annotated[str, Query(min_length=1)],
    timeframe: HistoricalInterval = HistoricalInterval.FIVE_MINUTE,
) -> OptionRecommendation:
    """Produce one actionable option-contract recommendation for
    `underlying`, driven by the AI Signal Engine's current read on it.

    Never places an order — see `app.options.recommendation`'s docstring
    for the full BUY/SELL/HOLD -> CE/PE/NO_TRADE flow.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        TradingModeNotEnabledError: 400, `Settings.trading_mode` is not
            `"OPTIONS"`.
        OptionsInfrastructureUnavailableError: 503, no `OptionChainService`
            is configured (the resolved broker isn't Angel One).
        UnsupportedUnderlyingError: 400, `underlying` isn't in
            `Settings.option_underlyings` — raised by `OptionChainService`
            itself, not duplicated here.
        NoHistoricalDataError: 404, the broker had no candle data for
            `underlying`.
        InvalidHistoricalDataError: 502, a returned candle failed
            validation.
        OptionChainFetchError: 503, the broker's option-chain fetch
            failed (network/API failure).
        InvalidOptionChainDataError: 502, the chain fetch succeeded but
            parsed to zero usable instruments.
        ExpiryNotFoundError: 404, no expiry satisfies the configured
            expiry mode.
        StrikeNotFoundError: 404, no strike is available to select from.
        PremiumUnavailableError: 502, the broker LTP lookup for the
            selected contract failed.
    """

    if settings.trading_mode != "OPTIONS":
        raise TradingModeNotEnabledError(
            "options recommendations require TRADING_MODE=OPTIONS", underlying=underlying
        )

    return await generate_option_recommendation(
        broker=broker,
        broker_name=settings.default_broker,
        option_chain_service=option_chain_service,
        underlying_resolver=underlying_resolver,
        underlying=underlying,
        timeframe=timeframe,
        strike_mode=StrikeMode(settings.option_strike_mode),
        expiry_mode=ExpiryMode(settings.option_expiry_mode),
    )


class PlaceOptionOrderRequest(BaseModel):
    """`POST /api/v1/options/paper/orders`'s request body: which
    underlying/timeframe to derive a recommendation from, how many lots
    to trade it in, and optional bracket premium levels.
    """

    model_config = ConfigDict(frozen=True)

    underlying: str = Field(min_length=1)
    timeframe: HistoricalInterval = HistoricalInterval.FIVE_MINUTE
    lots: int = Field(gt=0)
    stop_loss_premium: float | None = Field(default=None, gt=0)
    target_premium: float | None = Field(default=None, gt=0)


class OptionPaperOrderResponse(BaseModel):
    """`POST /api/v1/options/paper/orders`'s response: the recommendation
    that drove the decision, plus the resulting order — `order` is
    `None` when `recommendation.signal is OptionSignal.NO_TRADE`, since a
    HOLD signal is a legitimate, successful outcome (no order placed),
    not an error.
    """

    model_config = ConfigDict(frozen=True)

    recommendation: OptionRecommendation
    order: PaperOrder | None = None


class ExitOptionOrderRequest(BaseModel):
    """`POST /api/v1/options/paper/exit`'s request body."""

    model_config = ConfigDict(frozen=True)

    tradingsymbol: str = Field(min_length=1)
    reason: OptionExitReason
    quantity: int | None = Field(default=None, gt=0)


@router.post("/paper/orders", status_code=201)
async def place_option_paper_order(
    body: PlaceOptionOrderRequest,
    broker: Annotated[BrokerInterface, Depends(get_broker)],
    option_chain_service: Annotated[OptionChainService, Depends(get_option_chain_service_or_error)],
    underlying_resolver: Annotated[UnderlyingResolver, Depends(get_underlying_resolver_or_error)],
    risk_manager: Annotated[OptionRiskManager, Depends(get_option_risk_manager_or_error)],
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OptionPaperOrderResponse:
    """Derive an option-contract recommendation for `body.underlying` and,
    if it's actionable (not `NO_TRADE`), place a simulated PAPER-ONLY
    option order for it sized in `body.lots`.

    Never places a real broker order — see `app.options.paper_trading
    .enter_option_position`'s docstring. Setting `body.stop_loss_premium`/
    `body.target_premium` attaches a bracket exit pair to the entry (see
    `app.paper.order_manager`'s bracket-order design) — no separate call
    is needed to arm them.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        TradingModeNotEnabledError: 400, `Settings.trading_mode` is not
            `"OPTIONS"`.
        OptionsInfrastructureUnavailableError: 503, no `OptionChainService`/
            `OptionRiskManager` is configured (the resolved broker isn't
            Angel One).
        UnsupportedUnderlyingError: 400, `body.underlying` isn't in
            `Settings.option_underlyings`.
        NoHistoricalDataError: 404, the broker had no candle data for
            `body.underlying`.
        InvalidHistoricalDataError: 502, a returned candle failed
            validation.
        OptionChainFetchError: 503, the broker's option-chain fetch
            failed.
        InvalidOptionChainDataError: 502, the chain fetch succeeded but
            parsed to zero usable instruments.
        ExpiryNotFoundError: 404, no expiry satisfies the configured
            expiry mode.
        StrikeNotFoundError: 404, no strike is available to select from.
        PremiumUnavailableError: 502, the broker LTP lookup for the
            selected contract failed.
        InvalidLotQuantityError: 400, `body.lots` exceeds the configured
            maximum.
        OptionRiskLimitExceededError: 422, the candidate order fails one
            of the Phase 3 risk checks.
    """

    if settings.trading_mode != "OPTIONS":
        raise TradingModeNotEnabledError(
            "options paper trading requires TRADING_MODE=OPTIONS", underlying=body.underlying
        )

    recommendation = await generate_option_recommendation(
        broker=broker,
        broker_name=settings.default_broker,
        option_chain_service=option_chain_service,
        underlying_resolver=underlying_resolver,
        underlying=body.underlying,
        timeframe=body.timeframe,
        strike_mode=StrikeMode(settings.option_strike_mode),
        expiry_mode=ExpiryMode(settings.option_expiry_mode),
    )

    if recommendation.signal is OptionSignal.NO_TRADE:
        return OptionPaperOrderResponse(recommendation=recommendation, order=None)

    order = await enter_option_position(
        broker=broker,
        engine=engine,
        option_chain_service=option_chain_service,
        risk_manager=risk_manager,
        recommendation=recommendation,
        lots=body.lots,
        default_lot_size=settings.option_default_lot_size,
        max_lots_per_order=settings.option_max_lots_per_order,
        stop_loss_premium=body.stop_loss_premium,
        target_premium=body.target_premium,
    )
    return OptionPaperOrderResponse(recommendation=recommendation, order=order)


@router.post("/paper/exit")
async def exit_option_paper_order(
    body: ExitOptionOrderRequest,
    broker: Annotated[BrokerInterface, Depends(get_broker)],
    risk_manager: Annotated[OptionRiskManager, Depends(get_option_risk_manager_or_error)],
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PaperOrder:
    """Close (fully or partially) the open option position for
    `body.tradingsymbol`.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        TradingModeNotEnabledError: 400, `Settings.trading_mode` is not
            `"OPTIONS"`.
        OptionsInfrastructureUnavailableError: 503, no `OptionRiskManager`
            is configured (the resolved broker isn't Angel One).
        OptionPositionNotFoundError: 404, no open option position exists
            for `body.tradingsymbol`.
    """

    if settings.trading_mode != "OPTIONS":
        raise TradingModeNotEnabledError(
            "options paper trading requires TRADING_MODE=OPTIONS",
            underlying=None,
        )

    return await exit_option_position(
        broker=broker,
        engine=engine,
        risk_manager=risk_manager,
        tradingsymbol=body.tradingsymbol,
        reason=body.reason,
        quantity=body.quantity,
    )


@router.get("/paper/trades")
async def get_option_paper_trades(
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[OptionTradeHistoryEntry]:
    """List every closed option round-trip, enriched with underlying/
    exit-reason/strategy-provenance context.

    Deliberately has no equivalent `/paper/portfolio` or `/paper/positions`
    sibling here: the existing `GET /api/v1/paper/portfolio` and `GET
    /api/v1/paper/positions` already reflect option positions correctly
    (they're stored in the same `PaperTradingEngine`, just keyed on
    `Exchange.NFO` symbols) — a duplicate options-only route would only
    repeat the same data under a second URL.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        TradingModeNotEnabledError: 400, `Settings.trading_mode` is not
            `"OPTIONS"`.
    """

    if settings.trading_mode != "OPTIONS":
        raise TradingModeNotEnabledError(
            "options paper trading requires TRADING_MODE=OPTIONS", underlying=None
        )

    return await get_option_trade_history(engine, underlyings=settings.option_underlyings)


@router.get("/risk/status")
async def get_option_risk_status(
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
    risk_manager: Annotated[OptionRiskManager, Depends(get_option_risk_manager_or_error)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OptionRiskStatus:
    """A read-only snapshot of the Phase 3 options risk gate's current
    counters and configured limits — the Phase 4 Angular dashboard's
    "Risk Panel" section's one data source.

    Computes `current_premium_exposure` via
    `app.options.paper_trading.get_option_premium_exposure`, the exact
    same helper `enter_option_position`'s own risk check uses, so this
    status endpoint can never diverge from what a real order attempt
    would see.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        TradingModeNotEnabledError: 400, `Settings.trading_mode` is not
            `"OPTIONS"`.
        OptionsInfrastructureUnavailableError: 503, no `OptionRiskManager`
            is configured (the resolved broker isn't Angel One).
    """

    if settings.trading_mode != "OPTIONS":
        raise TradingModeNotEnabledError(
            "options risk status requires TRADING_MODE=OPTIONS", underlying=None
        )

    current = await get_option_premium_exposure(engine)
    return OptionRiskStatus(
        current_premium_exposure=current,
        max_premium_exposure=settings.option_max_premium_exposure,
        remaining_premium_capacity=max(0.0, settings.option_max_premium_exposure - current),
        daily_realized_pnl=risk_manager.daily_realized_pnl,
        max_daily_loss=settings.option_max_daily_loss,
        max_lots_per_order=settings.option_max_lots_per_order,
        max_premium_per_order=settings.option_max_premium_per_order,
    )


@router.get("/auto/status")
async def get_auto_options_status(
    orchestrator: Annotated[AutoOptionsOrchestrator, Depends(get_auto_options_orchestrator_or_error)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AutoOptionsStatus:
    """A read-only snapshot of Phase 5's automatic PAPER option-trading
    scheduler (`app.options.auto_trading.AutoOptionsOrchestrator`).

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        TradingModeNotEnabledError: 400, `Settings.trading_mode` is not
            `"OPTIONS"`.
        OptionsInfrastructureUnavailableError: 503, no
            `AutoOptionsOrchestrator` is configured (the resolved broker
            isn't Angel One).
    """

    if settings.trading_mode != "OPTIONS":
        raise TradingModeNotEnabledError(
            "auto options trading requires TRADING_MODE=OPTIONS", underlying=None
        )

    return orchestrator.status()


@router.post("/auto/start")
async def start_auto_options_trading(
    orchestrator: Annotated[AutoOptionsOrchestrator, Depends(get_auto_options_orchestrator_or_error)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AutoOptionsStatus:
    """Start the automatic PAPER option-trading loop. Idempotent: a
    second call while already running is a no-op, not an error.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        TradingModeNotEnabledError: 400, `Settings.trading_mode` is not
            `"OPTIONS"`.
        OptionsInfrastructureUnavailableError: 503, no
            `AutoOptionsOrchestrator` is configured (the resolved broker
            isn't Angel One).
    """

    if settings.trading_mode != "OPTIONS":
        raise TradingModeNotEnabledError(
            "auto options trading requires TRADING_MODE=OPTIONS", underlying=None
        )

    await orchestrator.start()
    return orchestrator.status()


@router.post("/auto/stop")
async def stop_auto_options_trading(
    orchestrator: Annotated[AutoOptionsOrchestrator, Depends(get_auto_options_orchestrator_or_error)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AutoOptionsStatus:
    """Stop the automatic PAPER option-trading loop, waiting for the
    current cycle (if any) to finish first. Idempotent. Does not touch
    any already-open option positions — exit them via
    `/api/v1/options/paper/exit` if that's what's wanted.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        TradingModeNotEnabledError: 400, `Settings.trading_mode` is not
            `"OPTIONS"`.
        OptionsInfrastructureUnavailableError: 503, no
            `AutoOptionsOrchestrator` is configured (the resolved broker
            isn't Angel One).
    """

    if settings.trading_mode != "OPTIONS":
        raise TradingModeNotEnabledError(
            "auto options trading requires TRADING_MODE=OPTIONS", underlying=None
        )

    await orchestrator.stop()
    return orchestrator.status()
