"""The Logs API: recent structured log activity for the dashboard's Logs page.

Backed by `app.core.logging`'s in-memory ring buffer — there is no log-
aggregation system in this project, so this is the only way anything
outside the process's own stdout can see recent log history. Added
because there was no existing way to satisfy the dashboard's Logs page
otherwise (Requirement: "Only add new APIs if absolutely necessary").
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.logging import LogEntry, get_recent_logs

router = APIRouter(prefix="/api/v1", tags=["logs"])


@router.get("/logs")
async def get_logs(limit: Annotated[int, Query(gt=0, le=2000)] = 200) -> list[LogEntry]:
    """The most recent `limit` log entries, oldest first."""

    return get_recent_logs(limit)
