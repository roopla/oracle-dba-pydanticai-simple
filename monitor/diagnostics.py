"""Approved read-only diagnostics used to enrich monitor recommendations.

The language model never writes or executes SQL. Each incident type can map to
reviewed query functions in oracle_core.queries. Diagnostics run only when a
new/reopened/severity-changed incident needs a fresh recommendation.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from monitor.models import DetectedIssue, IssueType
from oracle_core.queries import get_tablespace_diagnostics


async def _run_shared(function, /, *args, **kwargs):
    """Run one synchronous approved Oracle query off the event loop."""
    return await asyncio.to_thread(function, *args, **kwargs)


async def diagnose_tablespace_usage(issue: DetectedIssue) -> dict[str, Any]:
    """Collect capacity and autoextend evidence for one tablespace incident."""
    pdb_name = str(issue.details.get("con_name") or "").strip()
    tablespace_name = str(issue.details.get("tablespace_name") or "").strip()

    if not pdb_name or not tablespace_name:
        return {"diagnostic_error": "Tablespace incident is missing PDB or tablespace name."}

    files = await _run_shared(
        get_tablespace_diagnostics,
        pdb_name=pdb_name,
        tablespace_name=tablespace_name,
    )

    return {
        "tablespace_files": files,
    }


DiagnosticFunction = Callable[[DetectedIssue], Awaitable[dict[str, Any]]]

DIAGNOSTICS_BY_ISSUE: dict[IssueType, DiagnosticFunction] = {
    IssueType.TABLESPACE_USAGE: diagnose_tablespace_usage,
}


async def collect_diagnostics(issue: DetectedIssue) -> dict[str, Any]:
    """Collect approved evidence for an issue, if diagnostics are registered."""
    diagnostic = DIAGNOSTICS_BY_ISSUE.get(issue.issue_type)
    if diagnostic is None:
        return {}
    return await diagnostic(issue)
