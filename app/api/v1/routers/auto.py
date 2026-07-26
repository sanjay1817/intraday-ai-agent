"""The Automatic Paper Trading API: inspect, start, stop, and emergency-
stop `app.auto.orchestrator.AutoTradingOrchestrator`.

The orchestrator itself is always constructed at `app.state
.auto_orchestrator` during `app.main`'s lifespan (whether or not it's
actually running — see `Settings.auto_trading_enabled`, which only
controls whether it *starts* automatically), so these endpoints are
always available.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.auto.models import AutoTradingStatus
from app.auto.orchestrator import AutoTradingOrchestrator
from app.paper.models import PaperOrder

router = APIRouter(prefix="/api/v1/auto", tags=["auto"])


class EmergencyStopResponse(BaseModel):
    """`POST /api/v1/auto/emergency-stop`'s response: the orchestrator
    status after stopping, plus every closing order it placed while
    flattening open positions.
    """

    model_config = ConfigDict(frozen=True)

    status: AutoTradingStatus
    closed_orders: list[PaperOrder]


def get_orchestrator(request: Request) -> AutoTradingOrchestrator:
    """The shared `AutoTradingOrchestrator` constructed once at
    application startup (see `app.main`'s lifespan).
    """

    return request.app.state.auto_orchestrator  # type: ignore[no-any-return]


@router.get("/status")
async def get_status(
    orchestrator: Annotated[AutoTradingOrchestrator, Depends(get_orchestrator)],
) -> AutoTradingStatus:
    return orchestrator.status()


@router.post("/start")
async def start(
    orchestrator: Annotated[AutoTradingOrchestrator, Depends(get_orchestrator)],
) -> AutoTradingStatus:
    """Start the automatic trading loop. Idempotent: a second call
    while already running is a no-op, not an error.
    """

    await orchestrator.start()
    return orchestrator.status()


@router.post("/stop")
async def stop(
    orchestrator: Annotated[AutoTradingOrchestrator, Depends(get_orchestrator)],
) -> AutoTradingStatus:
    """Stop the automatic trading loop, waiting for the current cycle
    (if any) to finish first. Idempotent. Does not touch any already-
    open paper positions — cancel/flatten them via `/api/v1/paper/*`
    if that's what's wanted.
    """

    await orchestrator.stop()
    return orchestrator.status()


@router.post("/emergency-stop")
async def emergency_stop(
    orchestrator: Annotated[AutoTradingOrchestrator, Depends(get_orchestrator)],
) -> EmergencyStopResponse:
    """Stop the loop and immediately flatten every open paper position
    at the current market price — the dashboard's "Emergency Stop"
    button. Unlike `/stop`, this does touch open positions.
    """

    closed_orders = await orchestrator.emergency_stop()
    return EmergencyStopResponse(status=orchestrator.status(), closed_orders=closed_orders)
