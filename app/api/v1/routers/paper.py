"""The Paper Trading API: place/inspect simulated orders, positions,
portfolio, and trade history against `app.paper.engine.PaperTradingEngine`.

This engine is always available at `app.state.paper_engine`,
independent of whichever real broker `DEFAULT_BROKER` selects for the
Signal API — paper trading is a standalone system for exercising
strategies, not tied to a particular live broker being configured.
Price discovery for a new order (`current_price`) still needs a real
broker's `ltp()`, though, so this router also depends on
`app.state.broker` (the same shared adapter `app.api.v1.routers.signals`
uses) for that one purpose.

Only the six endpoints this milestone specifies are exposed here —
`app.paper.engine.PaperTradingEngine.cancel_order`/`modify_order` exist
and are fully tested, but this milestone's endpoint list doesn't call
for exposing them over REST yet.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.brokers.base import BrokerInterface
from app.paper.dto import PaperOrderRequest
from app.paper.engine import PaperTradingEngine
from app.paper.models import ClosedTrade, PaperOrder, PaperPosition, Portfolio, TradeMetadata

router = APIRouter(prefix="/api/v1/paper", tags=["paper"])


def get_paper_engine(request: Request) -> PaperTradingEngine:
    """The shared `PaperTradingEngine` constructed once at application
    startup (see `app.main`'s lifespan) — every request reads/writes the
    same in-memory paper account, exactly like a real broker's account.
    """

    return request.app.state.paper_engine  # type: ignore[no-any-return]


def get_broker(request: Request) -> BrokerInterface:
    """The shared broker adapter (see `app.api.v1.routers.signals`'s
    identical dependency) — used here only for `ltp()` price discovery
    when placing a paper order, never to place a real one.
    """

    return request.app.state.broker  # type: ignore[no-any-return]


class PlaceOrderRequest(BaseModel):
    """`POST /api/v1/paper/order`'s request body: the order itself plus
    optional strategy provenance (Requirement 5).

    Kept as a wrapper around `PaperOrderRequest` rather than adding a
    `metadata` field to it directly: `app.paper.models.TradeMetadata`
    lives in `app.paper.models`, which already imports `PaperOrderType`
    from `app.paper.dto` — importing back the other way would be a
    circular import.
    """

    model_config = ConfigDict(frozen=True)

    order: PaperOrderRequest
    metadata: TradeMetadata | None = None


class ResetRequest(BaseModel):
    """`POST /api/v1/paper/reset`'s request body. `initial_capital` is
    optional — omitted, the engine resets to its current starting
    capital (see `PortfolioManager.reset`'s own docstring for exactly
    what that means after a prior custom reset).
    """

    model_config = ConfigDict(frozen=True)

    initial_capital: float | None = Field(default=None, gt=0)


@router.post("/order", status_code=status.HTTP_201_CREATED)
async def place_order(
    body: PlaceOrderRequest,
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
    broker: Annotated[BrokerInterface, Depends(get_broker)],
) -> PaperOrder:
    """Place one simulated order, matched against the real broker's
    current LTP for `body.order.symbol`.

    The returned `PaperOrder` may be `COMPLETE` (filled outright),
    `OPEN`/`TRIGGER_PENDING` (resting), or `REJECTED` (e.g. insufficient
    paper cash) — all three are a "successful" placement in the sense
    that the order was validly accepted and recorded; only a malformed
    request (422) or a broker/market-data failure (502/503) raises.
    """

    quote = await broker.ltp(body.order.exchange, body.order.symbol)
    return await engine.place_order(
        body.order, current_price=quote.last_price, metadata=body.metadata
    )


@router.get("/orders")
async def get_orders(
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
) -> list[PaperOrder]:
    return await engine.get_orders()


@router.get("/positions")
async def get_positions(
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
) -> list[PaperPosition]:
    return await engine.get_positions()


@router.get("/portfolio")
async def get_portfolio(
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
) -> Portfolio:
    return await engine.get_portfolio()


@router.get("/trades")
async def get_trades(
    engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)],
) -> list[ClosedTrade]:
    return await engine.get_trades()


@router.post("/reset")
async def reset(
    body: ResetRequest, engine: Annotated[PaperTradingEngine, Depends(get_paper_engine)]
) -> Portfolio:
    """Reset every order/position/trade-history record and restore
    cash — returns the fresh `Portfolio` snapshot as confirmation.
    """

    await engine.reset(body.initial_capital)
    return await engine.get_portfolio()
