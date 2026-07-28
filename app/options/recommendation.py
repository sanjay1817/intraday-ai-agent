"""Phase 2 bridge: turns the AI Signal Engine's equity-index read into an
`OptionRecommendation` for the same underlying's option chain.

Every other file in `app.options` is deliberately broker/strategy-agnostic
infrastructure (see `app/options/__init__.py` and Phase 1's docs) — this
is the one file in the package allowed to import from `app.instructor`,
`app.strategy`, and `app.market`, because bridging "what does the AI
Signal Engine think about NIFTY the index" to "which NIFTY option
contract does that imply" is inherently a cross-package concern. Keeping
that coupling isolated here (instead of spread across `models.py`,
`option_chain_service.py`, etc.) means the rest of `app.options` stays
reusable by anything that only needs option-chain plumbing, independent
of this specific strategy-engine-driven recommendation flow.
"""

from __future__ import annotations

from datetime import datetime

from app.brokers.base import BrokerInterface
from app.domain.enums.trading import BrokerName, Exchange, HistoricalInterval
from app.domain.exceptions.broker import BrokerAPIError, BrokerConnectionError
from app.indicators.engine import IndicatorEngine
from app.instructor.recommendation import RecommendationAction, generate_recommendation
from app.market.ingestion import current_session_state, fetch_recent_candles
from app.options.exceptions import PremiumUnavailableError
from app.options.expiry_selector import ExpirySelector
from app.options.models import ExpiryMode, OptionType, StrikeMode
from app.options.option_chain_service import OptionChainService
from app.options.option_symbol_builder import OptionSymbolBuilder
from app.options.schemas import OptionRecommendation, OptionSignal
from app.options.strike_selector import StrikeSelector
from app.strategy.engine import StrategyEngine

_ACTION_TO_OPTION_TYPE = {
    RecommendationAction.BUY: OptionType.CE,
    RecommendationAction.SELL: OptionType.PE,
}

_ACTION_TO_OPTION_SIGNAL = {
    RecommendationAction.BUY: OptionSignal.BULLISH,
    RecommendationAction.SELL: OptionSignal.BEARISH,
}


async def generate_option_recommendation(
    *,
    broker: BrokerInterface,
    broker_name: BrokerName,
    option_chain_service: OptionChainService,
    underlying: str,
    timeframe: HistoricalInterval,
    strike_mode: StrikeMode,
    expiry_mode: ExpiryMode,
    now: datetime | None = None,
) -> OptionRecommendation:
    """Run the AI Signal Engine on `underlying` and translate its
    BUY/SELL/HOLD call into a recommended option contract (or `NO_TRADE`).

    Flow: fetch recent candles for `underlying` -> run the same
    Strategy-Engine-then-Instructor pipeline `app.api.v1.routers.signals`
    uses -> map BUY/SELL/HOLD to CE/PE/NO_TRADE -> (for BUY/SELL only)
    resolve an expiry and strike against `option_chain_service`'s chain
    -> build the trading symbol -> look up its premium.

    Args:
        broker: The adapter to fetch candles/premium through.
        broker_name: Identifies `broker` for error reporting (see
            `app.market.ingestion.fetch_recent_candles`'s docstring for
            why this isn't read off `broker` itself).
        option_chain_service: The shared `OptionChainService` to resolve
            `underlying`'s option chain against.
        underlying: The index/stock underlying to analyze (e.g. `"NIFTY"`)
            — also the `tradingsymbol` the candle fetch and strategy
            engine analyze directly, on `Exchange.NSE`.
        timeframe: The candle interval to analyze and recommend against.
        strike_mode: Which `StrikeSelector` mode to apply.
        expiry_mode: Which `ExpirySelector` mode to apply.
        now: The current time, for the instructor recommendation's
            `generated_at` and for deterministic testing; defaults to the
            real current time.

    Raises:
        NoHistoricalDataError: the broker had no candles for `underlying`.
        InvalidHistoricalDataError: a returned candle failed validation.
        UnsupportedUnderlyingError: `underlying` isn't in
            `option_chain_service`'s configured underlyings set.
        OptionChainFetchError: the broker chain fetch failed.
        InvalidOptionChainDataError: the chain fetch succeeded but parsed
            to zero usable instruments.
        ExpiryNotFoundError: no expiry satisfies `expiry_mode`.
        StrikeNotFoundError: no strike is available to select from.
        PremiumUnavailableError: the broker LTP lookup for the selected
            contract failed.
    """

    candles = await fetch_recent_candles(broker, broker_name, Exchange.NSE, underlying, timeframe)

    # Reuses the latest analyzed candle's close as the underlying LTP
    # instead of issuing a second `broker.ltp()` call: this keeps the
    # recommendation's reported LTP consistent with exactly the price
    # data the AI Signal Engine actually analyzed (a separate `ltp()`
    # call could race ahead to a newer tick than the candles reflect),
    # and avoids a redundant broker round trip for a value already in
    # hand.
    underlying_ltp = candles[-1].close

    session = current_session_state()
    confluence = StrategyEngine().analyze_symbol(
        underlying,
        Exchange.NSE,
        timeframe,
        candles,
        session,
        indicator_engine=IndicatorEngine(),
    )
    instructor_rec = generate_recommendation(confluence, Exchange.NSE, now=now)

    if instructor_rec.action is RecommendationAction.HOLD:
        # Nothing to trade: the option chain is never touched for HOLD,
        # since there is no direction to select a contract for.
        return OptionRecommendation(
            underlying=underlying,
            signal=OptionSignal.NO_TRADE,
            underlying_ltp=underlying_ltp,
            confidence=instructor_rec.confidence,
            reasoning=instructor_rec.reasoning,
            generated_at=instructor_rec.generated_at,
        )

    option_type = _ACTION_TO_OPTION_TYPE[instructor_rec.action]
    option_signal = _ACTION_TO_OPTION_SIGNAL[instructor_rec.action]

    chain = await option_chain_service.get_option_chain(underlying)
    expiry = ExpirySelector(default_mode=expiry_mode).select(chain.expiries())
    strikes = chain.strikes_for_expiry(expiry)
    strike = StrikeSelector(default_mode=strike_mode).select(strikes, underlying_ltp, option_type)

    # Built via `OptionSymbolBuilder` (constructed) rather than
    # `chain.instrument_for(...)` (chain-verified ground truth) — a
    # deliberate Phase 2 simplification, not an oversight. It mirrors
    # `OptionSymbolBuilder`'s own docstring caveat: prefer the chain's
    # real rows when correctness against the broker's exact listed
    # contract matters (e.g. before placing an order, which this
    # endpoint never does). Since this endpoint only ever recommends,
    # never places, the tiny risk of a builder/chain mismatch (e.g. a
    # symbol-format edge case) has no execution consequence — it is a
    # known, documented limitation to revisit if/when a future phase
    # wires this into order placement.
    tradingsymbol = OptionSymbolBuilder().build(underlying, expiry, strike, option_type)

    try:
        quote = await broker.ltp(Exchange.NFO, tradingsymbol)
    except (BrokerAPIError, BrokerConnectionError) as exc:
        raise PremiumUnavailableError(
            f"failed to fetch premium for {tradingsymbol}: {exc}", underlying=underlying
        ) from exc
    premium = quote.last_price

    reasoning = (
        f"{instructor_rec.reasoning} Selected {strike_mode.value} strike {strike:g} "
        f"{option_type.value} expiring {expiry.isoformat()}."
    )

    return OptionRecommendation(
        underlying=underlying,
        signal=option_signal,
        tradingsymbol=tradingsymbol,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        premium=premium,
        underlying_ltp=underlying_ltp,
        confidence=instructor_rec.confidence,
        reasoning=reasoning,
        generated_at=instructor_rec.generated_at,
    )
