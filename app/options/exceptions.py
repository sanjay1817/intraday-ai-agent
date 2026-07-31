"""Options domain exception hierarchy.

Mirrors `app.domain.exceptions.broker`'s pattern: a common base carrying
`underlying` context, with subclasses documenting exactly when each is
raised. `OptionChainService` is the boundary that translates broker
exceptions (`BrokerConnectionError`/`BrokerAPIError`) into
`OptionChainFetchError` — callers of this package never need to catch a
broker-specific exception type.
"""


class OptionsError(Exception):
    """Base class for every options-domain failure."""

    def __init__(self, message: str, *, underlying: str | None = None) -> None:
        self.underlying = underlying
        super().__init__(message)


class OptionChainFetchError(OptionsError):
    """Raised when fetching option-chain data from a broker fails.

    Wraps a `BrokerConnectionError`/`BrokerAPIError` raised by the
    underlying `OptionInstrumentSource` — callers of `OptionChainService`
    only ever see this type, never a raw broker exception.
    """


class InvalidOptionChainDataError(OptionsError):
    """Raised when a source's instrument rows parsed into an empty or
    otherwise nonsensical chain (e.g. zero option rows survived filtering
    for a requested underlying) even though the fetch itself succeeded.
    """


class UnsupportedUnderlyingError(OptionsError):
    """Raised when the requested underlying is not in the configured
    `Settings.option_underlyings` set that `OptionChainService` was built with.
    """


class ExpiryNotFoundError(OptionsError):
    """Raised when `ExpirySelector.select` has no expiry to return: either
    the input list is empty, or the requested mode can't be satisfied
    (e.g. `NEXT_WEEKLY` with fewer than two upcoming expiries).
    """


class StrikeNotFoundError(OptionsError):
    """Raised when `StrikeSelector.select` has no strike to return because
    the available-strikes list is empty.
    """


class PremiumUnavailableError(OptionsError):
    """Raised when fetching a selected option contract's LTP/premium from
    the broker fails.

    Wraps a `BrokerAPIError`/`BrokerConnectionError` raised by
    `BrokerInterface.ltp` — the same translate-at-the-boundary convention
    `OptionChainFetchError` already follows, so callers of
    `app.options.recommendation.generate_option_recommendation` never
    need to catch a broker-specific exception type.
    """


class UnderlyingInstrumentNotFoundError(OptionsError):
    """Raised by `app.options.underlying_resolver.UnderlyingResolver
    .resolve_historical_instrument` when no matching instrument-master row
    can be found for the requested underlying's own spot/index price —
    distinct from `UnsupportedUnderlyingError` (which means "not in
    `Settings.option_underlyings`"): this means "configured, but the
    broker's own instrument master has no matching row for it".
    """


class OptionContractNotFoundError(OptionsError):
    """Raised when the selected expiry/strike/option-type combination has
    no matching row in the option chain's own instrument list — i.e.
    `OptionChain.instrument_for(...)` returned `None`.

    Should not happen in practice: the expiry/strike being looked up are
    themselves derived from the SAME chain (`ExpirySelector`/`StrikeSelector`
    only ever choose from values `OptionChain.expiries()`/`strikes_for_expiry()`
    already returned), so a chain that's internally consistent can never hit
    this. Kept as a defensive guard — and to give `generate_option_recommendation`
    a domain exception instead of an `AssertionError`/unhandled `None` — in
    case a future change ever lets that invariant slip.
    """


class OptionsInfrastructureUnavailableError(OptionsError):
    """Raised when `app.state.option_chain_service` is `None` because the
    configured broker isn't Angel One (see `app.main`'s lifespan wiring),
    so there is no `OptionInstrumentSource` available to serve a chain.
    """


class TradingModeNotEnabledError(OptionsError):
    """Raised when the options recommendation endpoint is called while
    `Settings.trading_mode` is not `"OPTIONS"`.
    """


class OptionPositionNotFoundError(OptionsError):
    """Raised by `app.options.paper_trading.exit_option_position` when no
    open option position exists for the requested trading symbol on
    `Exchange.NFO` — there is nothing to exit.
    """


class InvalidLotQuantityError(OptionsError):
    """Raised when a requested `lots` count is not a positive integer, or
    exceeds `Settings.option_max_lots_per_order`.
    """


class OptionRiskLimitExceededError(OptionsError):
    """Raised when a candidate option order/exposure fails one of the
    Phase 3 risk checks (`app.options.risk.OptionRiskManager
    .check_order_allowed`).

    `reason` carries the specific limit breached (mirrors the pattern
    `app.domain.exceptions.broker.BrokerAPIError` uses to add its own
    extra keyword-only attributes on top of the base `OptionsError.__init__`).
    """

    def __init__(self, message: str, *, underlying: str | None = None, reason: str) -> None:
        self.reason = reason
        super().__init__(message, underlying=underlying)
