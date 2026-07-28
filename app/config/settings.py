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

    max_open_positions: int = Field(default=3, gt=0)
    max_daily_trades: int = Field(default=10, gt=0)
    max_daily_loss: float = Field(default=5000.0, gt=0)

    broker: BrokerSettings = BrokerSettings()

    @field_validator("paper_market_data_broker")
    @classmethod
    def _check_paper_market_data_broker_is_a_real_broker(cls, value: BrokerName) -> BrokerName:
        if value is BrokerName.PAPER:
            raise ValueError(
                "paper_market_data_broker cannot be 'paper' itself — it must name a real "
                "broker (angel_one, upstox, zerodha) for PaperBroker to source prices from"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings` singleton.

    Cached with `lru_cache` so environment parsing happens once per
    process; tests override it via `app.config.settings.get_settings.cache_clear()`
    plus dependency-override rather than mutating the singleton in place.
    """

    return Settings()
