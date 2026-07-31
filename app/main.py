"""The application factory and ASGI entrypoint.

`create_app()` only builds the FastAPI object graph — constructing the
`FastAPI` instance, registering exception handlers, and mounting routers
— and does no I/O of its own. Reading settings and configuring logging
happen inside `lifespan`, which only runs when an ASGI server actually
starts serving (uvicorn's startup sequence), not merely when this module
is imported. That split matters: importing `app.main` from a script, a
test, or a linter must stay fast and side-effect-free, never silently
reading environment variables or reconfiguring process-wide logging.

`lifespan` is deliberately a plain, sequential async function rather
than a plugin/registry system: adding a future resource (a database
engine, a Redis client, the scheduler, a websocket manager) means adding
one connect call before `yield` and its matching disconnect call after
`yield`, in reverse order — no structural change to this function, and
no new abstraction, is needed to accommodate that.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.routers.auto import router as auto_router
from app.api.v1.routers.health import router as health_router
from app.api.v1.routers.logs import router as logs_router
from app.api.v1.routers.options import router as options_router
from app.api.v1.routers.paper import router as paper_router
from app.api.v1.routers.signals import router as signals_router
from app.auto.models import AutoTradingConfig
from app.auto.orchestrator import AutoTradingOrchestrator
from app.brokers.angel_one import AngelOneAdapter
from app.brokers.angel_one_instruments import AngelOneInstrumentMaster
from app.brokers.factory import get_broker_adapter
from app.config.settings import get_settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.domain.enums.trading import HistoricalInterval
from app.options.auto_trading import AutoOptionsConfig, AutoOptionsOrchestrator
from app.options.models import ExpiryMode, StrikeMode
from app.options.option_chain_service import AngelOneOptionInstrumentSource, OptionChainService
from app.options.risk import OptionRiskManager
from app.options.underlying_resolver import AngelOneUnderlyingResolver
from app.paper.broker import PaperBroker
from app.paper.engine import PaperTradingEngine

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown.

    Settings are loaded first because logging configuration depends on
    them (`log_level`/`log_json`); logging is configured immediately
    after, before anything else runs, so every subsequent startup
    message — including this function's own — is rendered correctly
    instead of falling back to structlog's unconfigured default.

    Constructing `Settings()` validates it (a `Literal`/type mismatch in
    the environment raises `pydantic.ValidationError` here), so a
    misconfigured environment fails startup outright rather than serving
    requests against invalid configuration.

    Setting `app.state.ready` only at the very end of startup — and
    clearing it first on shutdown, before any teardown step — means
    `/ready` reports unready during both the startup and shutdown
    windows, not just the fully-stopped state.
    """

    settings = get_settings()
    configure_logging(settings)

    logger.info("application_starting", app_name=settings.app_name, app_env=settings.app_env)

    # Constructed, not logged in by default: Zerodha's `request_token` and
    # Upstox's `auth_code` are each single-use, obtained through that
    # broker's own out-of-band browser login redirect — calling `login()`
    # automatically here (or automatically per-request) would burn a
    # one-time credential on every restart, or on the second Signal API
    # call, respectively. Authenticating those adapters remains an operator
    # responsibility; `historical_data()` fails with a clean,
    # correctly-mapped `BrokerAPIError` if it's called before that's done.
    broker = get_broker_adapter(settings.default_broker, settings)
    app.state.broker = broker

    # Angel One is the one exception: SmartAPI's password+TOTP login is
    # fully programmatic and safely repeatable (unlike Zerodha/Upstox's
    # single-use browser tokens above), so it's safe to log it in
    # automatically here when `AUTO_LOGIN_ON_STARTUP=true` — which matters
    # for a long-running deployment (e.g. on AWS) where nothing else would
    # ever call `login()`. `PaperBroker.login()` delegates to its wrapped
    # market-data broker, so this also covers `DEFAULT_BROKER=paper` with
    # `PAPER_MARKET_DATA_BROKER=angel_one`. Any other resolved broker is
    # left untouched regardless of the flag, and a login failure (bad
    # credentials, network issue) is logged but does not prevent startup —
    # the same `BrokerAuthenticationError` `historical_data()` would have
    # raised anyway just surfaces on first use instead.
    login_target = broker.market_data_broker if isinstance(broker, PaperBroker) else broker
    if settings.auto_login_on_startup and isinstance(login_target, AngelOneAdapter):
        try:
            await broker.login()
            logger.info("broker_auto_login_succeeded", broker=login_target.broker_name.value)
        except Exception:
            logger.exception(
                "broker_auto_login_failed", broker=login_target.broker_name.value
            )

    # The Paper Trading Engine is always available, independent of
    # DEFAULT_BROKER: if `broker` already IS a `PaperBroker` (DEFAULT_BROKER
    # =paper), reuse its engine so `/api/v1/paper/*` and the Signal API
    # observe the exact same paper account rather than two independent
    # ones; otherwise construct a standalone engine, since paper trading
    # is a standalone system for exercising strategies, not tied to
    # whichever real broker is currently selected for live signals.
    app.state.paper_engine = (
        broker.engine
        if isinstance(broker, PaperBroker)
        else PaperTradingEngine(initial_capital=settings.paper_initial_capital)
    )

    # Always constructed, like `paper_engine` above — `auto_trading_enabled`
    # only controls whether it *starts* automatically; POST /api/v1/auto/start
    # can start it at runtime either way, so the endpoint must never find
    # `app.state.auto_orchestrator` missing.
    app.state.auto_orchestrator = AutoTradingOrchestrator(
        broker=broker,
        broker_name=settings.default_broker,
        paper_engine=app.state.paper_engine,
        config=AutoTradingConfig(
            symbols=settings.auto_symbols,
            confidence_threshold=settings.auto_confidence_threshold,
            max_open_positions=settings.max_open_positions,
            max_daily_trades=settings.max_daily_trades,
            max_daily_loss=settings.max_daily_loss,
        ),
    )
    if settings.auto_trading_enabled:
        await app.state.auto_orchestrator.start()

    # Options infrastructure (Phase 1 — see app/options and
    # docs/OPTIONS_PHASE1.md): only constructed when the resolved broker
    # (or, for PaperBroker, its wrapped market-data broker) is Angel One,
    # since `AngelOneOptionInstrumentSource` is the only
    # `OptionInstrumentSource` implemented so far. Left `None` for every
    # other broker configuration so this never breaks a Zerodha/Upstox
    # deployment. Reuses `login_target.instrument_master` (the adapter's
    # own `AngelOneInstrumentMaster`) rather than constructing a second
    # one, so the scrip master is downloaded/cached at most once. No
    # eager fetch and no background task — construction alone does no I/O.
    app.state.option_chain_service = None
    if isinstance(login_target, AngelOneAdapter):
        instrument_master = login_target.instrument_master
        if isinstance(instrument_master, AngelOneInstrumentMaster):
            app.state.option_chain_service = OptionChainService(
                underlyings=settings.option_underlyings,
                refresh_seconds=settings.option_chain_refresh_seconds,
                source=AngelOneOptionInstrumentSource(instrument_master),
            )
        else:
            logger.debug("option_chain_service_skipped_non_real_instrument_master")
    else:
        logger.debug("option_chain_service_skipped_non_angel_one_broker")

    # Bug fix (see docs/OPTIONS_PHASE2.md): constructed under the exact
    # same condition as `option_chain_service` above, reusing the same
    # `instrument_master` instance for the same reason (one scrip-master
    # download/cache serves every lookup) — `AngelOneUnderlyingResolver`
    # is the only `UnderlyingResolver` implemented so far. `None` in every
    # other broker configuration, mirroring `option_chain_service` exactly.
    app.state.underlying_resolver = None
    if isinstance(login_target, AngelOneAdapter):
        instrument_master = login_target.instrument_master
        if isinstance(instrument_master, AngelOneInstrumentMaster):
            app.state.underlying_resolver = AngelOneUnderlyingResolver(instrument_master)
        else:
            logger.debug("underlying_resolver_skipped_non_real_instrument_master")
    else:
        logger.debug("underlying_resolver_skipped_non_angel_one_broker")

    # Phase 3 (see app/options/risk.py, app/options/paper_trading.py and
    # docs/OPTIONS_PHASE3.md): constructed under the exact same condition
    # as `option_chain_service` above — option paper trading has no
    # usable premium/chain source without one, so there is nothing
    # useful for this gate to guard when the resolved broker isn't Angel
    # One. `None` in every other broker configuration, mirroring
    # `option_chain_service` exactly.
    app.state.option_risk_manager = (
        OptionRiskManager(
            max_lots_per_order=settings.option_max_lots_per_order,
            max_premium_per_order=settings.option_max_premium_per_order,
            max_premium_exposure=settings.option_max_premium_exposure,
            max_daily_loss=settings.option_max_daily_loss,
        )
        if app.state.option_chain_service is not None
        else None
    )

    # Phase 5 (see app/options/auto_trading.py and docs/OPTIONS_PHASE5.md):
    # constructed under the exact same condition as `option_chain_service`/
    # `option_risk_manager` above — the automatic options scheduler has
    # nothing to scan without a chain/risk gate. `None` in every other
    # broker configuration, mirroring both exactly. `auto_options_enabled`
    # only controls whether it *starts* automatically here; POST
    # /api/v1/options/auto/start can start it at runtime either way,
    # exactly like `auto_trading_enabled` does for the equity orchestrator.
    app.state.auto_options_orchestrator = (
        AutoOptionsOrchestrator(
            broker=broker,
            broker_name=settings.default_broker,
            paper_engine=app.state.paper_engine,
            option_chain_service=app.state.option_chain_service,
            underlying_resolver=app.state.underlying_resolver,
            risk_manager=app.state.option_risk_manager,
            config=AutoOptionsConfig(
                underlyings=settings.option_underlyings,
                timeframe=HistoricalInterval.FIVE_MINUTE,
                strike_mode=StrikeMode(settings.option_strike_mode),
                expiry_mode=ExpiryMode(settings.option_expiry_mode),
                confidence_threshold=settings.auto_option_confidence_threshold,
                max_open_positions=settings.auto_max_open_option_positions,
                lots_per_trade=settings.auto_option_lots_per_trade,
                scan_interval_seconds=settings.auto_option_scan_interval_seconds,
                exit_time=settings.auto_option_exit_time,
                stop_loss_percent=settings.auto_option_stop_loss_percent,
                target_percent=settings.auto_option_target_percent,
            ),
            default_lot_size=settings.option_default_lot_size,
            max_lots_per_order=settings.option_max_lots_per_order,
        )
        if app.state.option_chain_service is not None
        else None
    )
    if settings.auto_options_enabled and settings.trading_mode == "OPTIONS":
        await app.state.auto_options_orchestrator.start()
    else:
        logger.debug("auto_options_orchestrator_not_started")

    # Future startup steps (database engine connect, Redis client
    # connect, scheduler start, websocket manager start) belong here, in
    # dependency order, before `app.state.ready` is set.

    app.state.ready = True
    logger.info("application_ready")

    try:
        yield
    finally:
        app.state.ready = False
        logger.info("application_stopping")

        await app.state.auto_orchestrator.stop()
        if app.state.auto_options_orchestrator is not None:
            await app.state.auto_options_orchestrator.stop()
        await broker.close()

        # Future shutdown steps belong here, in the reverse order of the
        # startup steps above, before the final log line.

        logger.info("application_stopped")


def create_app() -> FastAPI:
    """Build the FastAPI application.

    The title/version below are static rather than settings-derived on
    purpose: reading settings here would give `create_app()` — and thus
    importing this module — an I/O side effect. The application's live
    identity (name, environment, uptime) is reported by `/health`, which
    reads settings per-request via dependency injection instead.
    """

    app = FastAPI(
        title="AI Intraday Trading Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ready = False

    # The Angular dashboard (Phase 4) is served from a different origin
    # (the Angular dev server, or a static host) than this API, so it
    # needs CORS enabled to call it at all. `allow_origins=["*"]` is
    # safe specifically because `allow_credentials` is False — this API
    # has no cookie/session-based auth (see the architecture audit: no
    # auth layer exists yet), so there's no credential to leak cross-
    # origin; it's a static, settings-independent value for the same
    # reason title/version above are (see this function's docstring).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(signals_router)
    app.include_router(paper_router)
    app.include_router(auto_router)
    app.include_router(logs_router)
    app.include_router(options_router)

    return app


app = create_app()
