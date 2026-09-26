"""Monitor sub-application: REST API + dashboard page.

Starlette does not run the lifespan of mounted sub-apps, so startup() and
shutdown() are called explicitly by agent_app.web_combined.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from monitor.models import (
    AcknowledgmentFilter,
    IssueStatus,
    IssueType,
    Severity,
    SortOrder,
    StatusFilter,
)
from monitor.storage import acknowledge_issue, get_issue, init_db, list_issues

logger = logging.getLogger("monitor.api")

_STATIC_DIR = Path(__file__).parent / "static"

_stop_event: asyncio.Event | None = None
_poller_task: asyncio.Task | None = None


async def startup() -> None:
    """Called by the root app's lifespan on startup."""
    global _stop_event, _poller_task
    from monitor.poller import poll_forever

    await init_db()
    _stop_event = asyncio.Event()
    _poller_task = asyncio.create_task(poll_forever(_stop_event))
    logger.info("Monitor subsystem started")


async def shutdown() -> None:
    """Called by the root app's lifespan on shutdown."""
    if _stop_event is not None:
        _stop_event.set()
    if _poller_task is not None:
        await _poller_task
    logger.info("Monitor subsystem stopped")


monitor_app = FastAPI(title="Oracle DB Monitor", docs_url="/api/docs")


class AckRequest(BaseModel):
    acknowledged_by: str = Field(
        default="dashboard-user",
        min_length=1,
        max_length=100,
    )


@monitor_app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(_STATIC_DIR / "dashboard.html")


@monitor_app.get("/api/issues")
async def api_list_issues(
    status: StatusFilter = StatusFilter.ACTIVE,
    acknowledgment: AcknowledgmentFilter = AcknowledgmentFilter.ALL,
    severity: Severity | None = None,
    issue_type: IssueType | None = None,
    search: str | None = Query(default=None, max_length=200),
    sort: SortOrder = SortOrder.NEWEST,
    limit: int = Query(default=100, ge=1, le=500),
):
    status_value = (
        None if status == StatusFilter.ALL else IssueStatus(status.value)
    )
    acknowledged_value = {
        AcknowledgmentFilter.ALL: None,
        AcknowledgmentFilter.ACKNOWLEDGED: True,
        AcknowledgmentFilter.UNACKNOWLEDGED: False,
    }[acknowledgment]

    return await list_issues(
        status=status_value,
        acknowledged=acknowledged_value,
        severity=severity,
        issue_type=issue_type,
        search=search,
        sort_order=sort,
        limit=limit,
    )


@monitor_app.get("/api/issues/{issue_id}")
async def api_get_issue(issue_id: int):
    record = await get_issue(issue_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return record


@monitor_app.post("/api/issues/{issue_id}/acknowledge")
async def api_acknowledge_issue(issue_id: int, body: AckRequest):
    record = await acknowledge_issue(issue_id, body.acknowledged_by.strip())
    if record is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    if record.status == IssueStatus.RESOLVED and not record.acknowledged:
        raise HTTPException(
            status_code=409,
            detail="Resolved issues cannot be acknowledged",
        )
    return record


@monitor_app.get("/api/health")
async def health():
    poller_alive = _poller_task is not None and not _poller_task.done()
    return {"status": "ok", "poller_running": poller_alive}
