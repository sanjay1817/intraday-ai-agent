"""The Signal API: the on-demand HTTP endpoint that produces one
intraday BUY/SELL/HOLD recommendation for a symbol.

This is the only new route this milestone adds. It is a pure,
synchronous-per-request pipeline — fetch recent candles, compute
indicators, run every strategy, merge into confluence, synthesize a
recommendation — with no background process and no persisted state,
matching the Intraday AI Instructor's on-demand design: a human
requests a fresh read on a symbol when they want one, rather than a
continuously-running monitoring service.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.brokers.base import BrokerInterface
from app.config.settings import Settings, get_settings
from app.domain.enums.trading import Exchange, HistoricalInterval
from app.indicators.engine import IndicatorEngine
from app.instructor.recommendation import InstructorRecommendation, generate_recommendation
from app.market.ingestion import current_session_state, fetch_recent_candles
from app.strategy.engine import StrategyEngine

router = APIRouter(prefix="/api/v1", tags=["signals"])


def get_broker(request: Request) -> BrokerInterface:
    """The shared broker adapter constructed once at application startup
    (see `app.main`'s lifespan) — not reconstructed per request, since
    login/connection setup isn't free.
    """

    return request.app.state.broker  # type: ignore[no-any-return]


@router.get("/signals")
async def get_signal(
    broker: Annotated[BrokerInterface, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_settings)],
    exchange: Exchange,
    tradingsymbol: Annotated[str, Query(min_length=1)],
    interval: HistoricalInterval = HistoricalInterval.FIVE_MINUTE,
) -> InstructorRecommendation:
    """Produce one actionable intraday recommendation for `tradingsymbol`.

    `tradingsymbol` must already be in whatever form the configured
    broker's adapter expects (a numeric instrument token for Angel
    One/Zerodha, an `"EXCHANGE|ISIN"`-shaped key for Upstox) — this
    endpoint does no symbol resolution of its own; see the architecture
    audit's note on this standing gap.

    Raises (mapped to HTTP responses by `app.core.exception_handlers`):
        NoHistoricalDataError: 404, the broker had no data for this
            symbol/range.
        InvalidHistoricalDataError / BrokerAPIError / BrokerConnectionError:
            502/503, the broker call itself failed.
        InvalidOHLCVDataError / UnknownIndicatorError: 400/422, propagated
            from indicator computation (should not occur in practice —
            `app.strategy.engine` only ever requests registered
            indicators against non-empty candle data).
    """

    candles = await fetch_recent_candles(
        broker, settings.default_broker, exchange, tradingsymbol, interval
    )
    session = current_session_state()

    engine = StrategyEngine()
    confluence = engine.analyze_symbol(
        tradingsymbol,
        exchange,
        interval,
        candles,
        session,
        indicator_engine=IndicatorEngine(),
    )

    return generate_recommendation(confluence, exchange)
