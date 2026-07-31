"""Health, readiness, and liveness endpoints.

Three distinct probes, matching the standard container-orchestration
convention (Kubernetes, ECS, etc.) rather than one generic endpoint that
conflates them:

- `/live` (liveness): is the process itself still running and able to
  handle a request at all? If this handler executes, the answer is
  always yes — a liveness probe exists so an orchestrator can detect and
  restart a genuinely hung/deadlocked process, not to report business
  status.
- `/ready` (readiness): has application startup actually finished? A
  process can be alive but not yet ready (still running its startup
  sequence), and routing real traffic to it before then would fail.
  Reflects `app.state.ready`, set by the lifespan in `app.main` once
  startup completes and cleared on shutdown. This deliberately doesn't
  fabricate a check against a database/Redis/broker connection — none of
  those exist in this codebase yet (see the architecture audit); it only
  reports what's actually true today, and gains real dependency checks
  the moment those layers exist.
- `/health`: a general status snapshot for humans/dashboards/monitoring
  (app identity, environment, uptime, plus a handful of options-trading
  configuration/availability flags the Phase 4 Angular dashboard's
  "System Status" section reads) — informational, not itself a pass/fail
  probe like the two above.
"""

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.config.settings import Settings, get_settings

router = APIRouter(tags=["health"])

#: Process start time, captured once at import — the monotonic clock is
#: immune to system-clock adjustments (NTP sync, manual changes), which
#: a wall-clock-based uptime calculation would otherwise be vulnerable to.
_PROCESS_STARTED_AT = time.monotonic()


def _uptime_seconds() -> float:
    return time.monotonic() - _PROCESS_STARTED_AT


@router.get("/live")
def liveness() -> dict[str, str]:
    """Liveness probe: reaching this line at all means the process is alive."""

    return {"status": "alive"}


@router.get("/ready")
def readiness(request: Request) -> JSONResponse:
    """Readiness probe: reflects whether application startup has completed.

    Returns 503 (not 200-with-a-false-flag) when not ready, so a load
    balancer or orchestrator's own readiness-probe logic — which checks
    the status code, not the body — correctly stops routing traffic here
    without needing special-case handling.
    """

    is_ready = bool(getattr(request.app.state, "ready", False))
    if is_ready:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ready"})
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"status": "not_ready"}
    )


@router.get("/health")
def health(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """A general status snapshot: app identity, environment, uptime, and a
    few options-trading configuration/availability fields.

    The options-related fields (`trading_mode`, `default_broker`,
    `options_infrastructure_available`, `option_underlyings`,
    `option_default_lot_size`) are additive, Phase 4 Angular dashboard
    conveniences — read straight off already-injected `settings` and off
    `request.app.state` (mirroring the existing defensive
    `getattr(..., False)` read for `ready` above), never a new
    dependency or I/O call, so this endpoint stays cheap and synchronous
    exactly as it was before.
    """

    return {
        "status": "ok",
        "app_name": settings.app_name,
        "app_env": settings.app_env,
        "uptime_seconds": round(_uptime_seconds(), 3),
        "ready": bool(getattr(request.app.state, "ready", False)),
        "trading_mode": settings.trading_mode,
        "default_broker": settings.default_broker.value,
        "options_infrastructure_available": bool(
            getattr(request.app.state, "option_chain_service", None) is not None
        ),
        "option_underlyings": list(settings.option_underlyings),
        "option_default_lot_size": settings.option_default_lot_size,
    }
