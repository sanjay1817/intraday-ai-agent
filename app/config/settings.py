"""Application settings.

Centralizes every environment-driven value behind a single `Settings`
model so no other module reads `os.environ` directly. Broker credentials
are grouped under `BrokerSettings` because the broker adapters
(`app.brokers`) are the first consumers of this module; general
infrastructure settings (database, Redis, JWT) are added here as those
layers are built.

Nested env vars use `__` as the delimiter, e.g. a Zerodha API key is read
from `BROKER__ZERODHA__API_KEY` (see `.env.example`).
"""

from datetime import time
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums.trading import BrokerName


class AngelOneCredentials(BaseModel):
    """Credentials and connection details for Angel One SmartAPI.

    `totp_secret` is the base32 secret used to generate the time-based
    one-time password SmartAPI requires at login (not a static OTP).
    """

    client_code: str = ""
    password: str = ""
    totp_secret: str = ""
    api_key: str = ""
    base_url: str = "https://apiconnect.angelbroking.com"


class UpstoxCredentials(BaseModel):
    """Credentials for Upstox's OAuth2 authorization-code login flow.

    Upstox has no programmatic re-authentication: `auth_code` is the
    one-time `code` obtained after the user completes the browser
    authorization redirect, and must be refreshed (by repeating that
    redirect) whenever it or the resulting access token expires.
    """

    api_key: str = ""
    api_secret: str = ""
    redirect_uri: str = ""
    auth_code: str = ""
    base_url: str = "https://api.upstox.com/v2"


class ZerodhaCredentials(BaseModel):
    """Credentials for Zerodha Kite Connect's request-token login flow.

    Kite Connect has no password-based login via the public API: the
    user must complete a browser login and Kite redirects back with a
    one-time `request_token`, which this adapter exchanges for an
    `access_token`. `refresh_token` support requires a Kite Connect
    "extended" app; standard apps must repeat the browser flow to get a
    new `request_token` once the access token expires.
    """

    api_key: str = ""
    api_secret: str = ""
    request_token: str = ""
    base_url: str = "https://api.kite.trade"


class BrokerSettings(BaseModel):
    """Cross-broker HTTP behavior plus each broker's credential block.

    Retry/timeout values live here (not hard-coded in adapters) so ops can
    tune them per environment without a code change.
    """

    request_timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    retry_backoff_seconds: float = Field(default=0.5, gt=0)
    retry_max_backoff_seconds: float = Field(default=8.0, gt=0)

    angel_one: AngelOneCredentials = AngelOneCredentials()
    upstox: UpstoxCredentials = UpstoxCredentials()
    zerodha: ZerodhaCredentials = ZerodhaCredentials()


class Settings(BaseSettings):
    """Root application settings, populated from environment / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Intraday Trading Agent"
    app_env: Literal["development", "testing", "staging", "production"] = "development"
    debug: bool = True
    log_level: str = "INFO"
    log_json: bool = False

    #: Directory for the dated buy/sell trade log (one file per calendar
    #: day, e.g. `trades-2026-08-06.log`), kept separate from the main
    #: application log so order activity can be tracked/archived per day.
    trade_log_dir: str = "logs/trades"

    #: Which configured broker (see `broker` below) `app.brokers.factory
    #: .get_broker_adapter` constructs when a caller (e.g. the Signal API)
    #: doesn't specify one explicitly. Defaults to Angel One — the only
    #: broker this codebase can authenticate programmatically end-to-end
    #: (password + TOTP, no browser redirect — see `AngelOneAdapter.login`)
    #: and the only one this project has live-verified (see
    #: `paper_market_data_broker` below, which has always defaulted to it
    #: for the same reason). Overriding it (`DEFAULT_BROKER=zerodha`)
    #: requires no code change. An unrecognized value fails fast with a
    #: clear Pydantic validation error naming every valid `BrokerName`
    #: member, the same way every other enum-typed setting in this
    #: codebase already validates.
    default_broker: BrokerName = BrokerName.ANGEL_ONE

    #: Which *real* broker `app.paper.broker.PaperBroker` delegates market
    #: data (`ltp`/`historical_data`/`start_websocket`) to — paper trading
    #: has no price feed of its own. Kept separate from `default_broker`
    #: so `DEFAULT_BROKER=paper` doesn't have to also mean "and use paper
    #: for its own market data", which would be circular. Defaults to
    #: Angel One since it's this project's only live-verified broker.
    paper_market_data_broker: BrokerName = BrokerName.ANGEL_ONE

    #: Whether `app.main`'s lifespan calls `login()` on the constructed
    #: broker automatically at startup. Defaults to `False` because for
    #: Zerodha/Upstox that would be actively harmful — their `request_token`
    #: /`auth_code` are single-use, obtained through a manual browser
    #: redirect, and would be burned on every process restart (see
    #: `app.main.lifespan`'s docstring). It is safe to enable
    #: (`AUTO_LOGIN_ON_STARTUP=true`) when the resolved broker is Angel
    #: One (directly via `DEFAULT_BROKER=angel_one`, or indirectly via
    #: `DEFAULT_BROKER=paper` + `PAPER_MARKET_DATA_BROKER=angel_one`)
    #: since SmartAPI's password+TOTP login is fully programmatic and
    #: repeatable — see `AngelOneAdapter.login`. `app.main` only attempts
    #: the auto-login when the resolved broker actually is Angel One,
    #: regardless of this flag, so turning it on against a
    #: Zerodha/Upstox-configured environment is a no-op rather than a
    #: credential-burning surprise.
    auto_login_on_startup: bool = False

    #: Starting cash for `app.paper.engine.PaperTradingEngine`, both the
    #: instance `app.main`'s lifespan constructs at `app.state.paper_engine`
    #: and the default `POST /api/v1/paper/reset` restores.
    paper_initial_capital: float = Field(default=100_000.0, gt=0)

    #: Whether `app.auto.orchestrator.AutoTradingOrchestrator` starts
    #: automatically during `app.main`'s lifespan. `POST`/`GET
    #: /api/v1/auto/*` can start/stop/inspect it at runtime regardless of
    #: this value — it only controls the state at process startup.
    auto_trading_enabled: bool = False

    #: Trading symbols the orchestrator polls, all on the same exchange
    #: (`app.auto.models.AutoTradingConfig.exchange`, default NSE) —
    #: e.g. `AUTO_SYMBOLS=["SBIN-EQ","RELIANCE-EQ"]` (a JSON array, the
    #: format pydantic-settings expects for a `list[str]` env var).
    auto_symbols: list[str] = Field(default_factory=list)

    #: Minimum `InstructorRecommendation.confidence` the orchestrator
    #: acts on — both for a new entry and for treating a fresh signal
    #: as a reversal strong enough to exit an existing position.
    auto_confidence_threshold: float = Field(default=60.0, ge=0, le=100)

    #: Minimum number of the Strategy Engine's registered strategies
    #: (currently 3 — `EMATrendStrategy`/`RSIMACDReversalStrategy`/
    #: `VWAPVolumeBreakoutStrategy`) that must agree on a direction before
    #: the orchestrator acts on it as a new entry — "2 of 3" by default.
    #: `_merge_into_confluence`'s majority vote already guarantees a
    #: BULLISH/BEARISH `direction` has *more* agreeing strategies than
    #: conflicting ones, but with 3 strategies that only requires 1 of 3
    #: when the other two return NONE (1 > 0); this closes that gap.
    auto_min_confirmations: int = Field(default=2, ge=1)

    max_open_positions: int = Field(default=3, gt=0)
    max_daily_trades: int = Field(default=10, gt=0)
    max_daily_loss: float = Field(default=5000.0, gt=0)

    #: Reserved for a future phase that actually routes orders through
    #: options (`app.options`). Phase 1 (option-chain fetch/cache/expiry
    #: /strike selection only) never reads this to change any existing
    #: behavior — it exists now so later phases don't need a settings
    #: migration to introduce it.
    trading_mode: Literal["EQUITY", "OPTIONS"] = "EQUITY"

    #: Underlyings `app.options.OptionChainService` is configured to serve
    #: option chains for, e.g. `OPTION_UNDERLYINGS=["NIFTY","BANKNIFTY"]`
    #: (a JSON array, same format as `auto_symbols` above). Defaults to
    #: the three most liquid NSE index-option underlyings.
    option_underlyings: list[str] = Field(
        default_factory=lambda: ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    )

    #: Default `app.options.models.StrikeMode` `app.options.StrikeSelector`
    #: applies when a caller doesn't specify one explicitly.
    option_strike_mode: Literal["ATM", "ITM", "OTM"] = "ATM"

    #: Default `app.options.models.ExpiryMode` `app.options.ExpirySelector`
    #: applies when a caller doesn't specify one explicitly.
    option_expiry_mode: Literal["NEAREST_WEEKLY", "NEXT_WEEKLY", "MONTHLY"] = "NEAREST_WEEKLY"

    #: How long `app.options.OptionChainService` treats a cached option
    #: chain as fresh before re-fetching from the broker. 30s balances
    #: staleness against re-filtering the (already locally cached) scrip
    #: master on every call during a burst of requests.
    option_chain_refresh_seconds: float = Field(default=30.0, gt=0)

    #: Fallback lot size `app.options.lot_sizing.resolve_lot_size` uses
    #: only when the resolved `OptionInstrument.lot_size` from the live
    #: chain is unavailable (`None`). The chain's own value — Angel One
    #: scrip-master `lotsize`, ground truth from the broker's own
    #: instrument master — is always preferred when present; this is a
    #: documented last resort, not a broker-specific override.
    option_default_lot_size: int = Field(default=50, gt=0)

    #: Maximum lots a single Phase 3 paper option order may request —
    #: enforced by `app.options.risk.OptionRiskManager.check_order_allowed`.
    option_max_lots_per_order: int = Field(default=10, gt=0)

    #: Maximum premium value (`quantity * premium`) a single Phase 3
    #: paper option order may commit — enforced by
    #: `app.options.risk.OptionRiskManager.check_order_allowed`.
    option_max_premium_per_order: float = Field(default=50_000.0, gt=0)

    #: Maximum total premium value across every open option position
    #: (existing exposure plus the candidate order) Phase 3 paper option
    #: trading may carry at once — enforced by
    #: `app.options.risk.OptionRiskManager.check_order_allowed`.
    option_max_premium_exposure: float = Field(default=200_000.0, gt=0)

    #: Maximum realized option P&L loss for one IST trading day before
    #: `app.options.risk.OptionRiskManager` locks out further entries —
    #: independent of `max_daily_loss` above, which is
    #: `app.auto.risk.AutoRiskManager`'s equivalent for Auto Trading
    #: equity strategies, not shared with Phase 3 options.
    option_max_daily_loss: float = Field(default=10_000.0, gt=0)

    broker: BrokerSettings = BrokerSettings()

    #: Whether `app.options.auto_trading.AutoOptionsOrchestrator` starts
    #: automatically during `app.main`'s lifespan (only when
    #: `trading_mode` is also `"OPTIONS"` — see that module's wiring in
    #: `app.main`). Mirrors `auto_trading_enabled`'s exact semantics for
    #: the equity orchestrator: `POST`/`GET /api/v1/options/auto/*` can
    #: start/stop/inspect it at runtime regardless of this value.
    auto_options_enabled: bool = False

    #: IST wall-clock time at/after which `AutoOptionsOrchestrator`
    #: force-exits every open paper option position it is tracking,
    #: regardless of premium — mirrors `app.auto.models
    #: .AutoTradingConfig.square_off_time`'s exact purpose for options.
    auto_option_exit_time: time = Field(default=time(15, 20))

    #: Orchestrator-level cap on simultaneously open option positions —
    #: a plain position-count gate this orchestrator checks itself
    #: (`app.options.risk.OptionRiskManager.check_order_allowed` was
    #: deliberately never given an `open_position_count` parameter; see
    #: `app.options.auto_trading`'s module docstring).
    auto_max_open_option_positions: int = Field(default=3, gt=0)

    #: How often (seconds) `AutoOptionsOrchestrator` scans every
    #: configured underlying for a fresh recommendation and re-checks
    #: every open position's exit conditions.
    auto_option_scan_interval_seconds: float = Field(default=30.0, gt=0)

    #: Minimum `OptionRecommendation.confidence` `AutoOptionsOrchestrator`
    #: acts on for a new entry — the options equivalent of
    #: `auto_confidence_threshold` above.
    auto_option_confidence_threshold: float = Field(default=60.0, ge=0, le=100)

    #: The options equivalent of `auto_min_confirmations` above —
    #: `AutoOptionsOrchestrator` requires at least this many of the
    #: Strategy Engine's registered strategies to agree before entering
    #: a new option position.
    auto_option_min_confirmations: int = Field(default=2, ge=1)

    #: Fixed lot count `AutoOptionsOrchestrator` requests for every entry
    #: it places — no dynamic position-sizing-by-capital logic in this
    #: phase (see `docs/OPTIONS_PHASE5.md`).
    auto_option_lots_per_trade: int = Field(default=1, gt=0)

    #: Percentage of entry premium below which `AutoOptionsOrchestrator`
    #: exits a tracked position as a stop-loss — a deliberate
    #: premium-percentage simplification with no Greeks/IV modeling (see
    #: `app.options.auto_trading`'s module docstring).
    auto_option_stop_loss_percent: float = Field(default=30.0, gt=0, le=100)

    #: Percentage of entry premium above which `AutoOptionsOrchestrator`
    #: exits a tracked position as a target hit — the same
    #: premium-percentage simplification as `auto_option_stop_loss_percent`.
    auto_option_target_percent: float = Field(default=50.0, gt=0)

    #: Directory `app.backtest.session.BacktestOrchestrator` caches
    #: downloaded historical candles under and persists completed
    #: `BacktestResult`s to — this codebase has no database (see
    #: `app.database`'s docstring), so backtest results are file-based,
    #: matching `trade_log_dir`'s own convention.
    backtest_data_dir: str = "data/backtest"

    @field_validator("paper_market_data_broker")
    @classmethod
    def _check_paper_market_data_broker_is_a_real_broker(cls, value: BrokerName) -> BrokerName:
        if value is BrokerName.PAPER:
            raise ValueError(
                "paper_market_data_broker cannot be 'paper' itself — it must name a real "
                "broker (angel_one, upstox, zerodha) for PaperBroker to source prices from"
            )
        return value

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _uppercase_trading_mode(cls, value: object) -> object:
        """Accept `TRADING_MODE=equity`/`TRADING_MODE=options` (lowercase,
        as the Phase 2 docs/examples use) in addition to the `Literal`'s
        canonical uppercase values — additive, and the default (`"EQUITY"`)
        and every existing all-uppercase usage are unaffected.
        """

        if isinstance(value, str):
            return value.upper()
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings` singleton.

    Cached with `lru_cache` so environment parsing happens once per
    process; tests override it via `app.config.settings.get_settings.cache_clear()`
    plus dependency-override rather than mutating the singleton in place.
    """

    return Settings()
