"""Oracle health checks for the monitor.

Design notes:
- Reuses reviewed oracle_core.queries functions via asyncio.to_thread, so
  monitor checks and MCP tools share one SQL/query implementation.
- All checks run against the CDB root (settings.oracle_cdb_name) using
  container-aware views (CDB_DATA_FILES, V$SESSION with CON_ID, etc.),
  so a single connection point covers every PDB.
- Only free V$/CDB_ views are used — no DBA_HIST_* (AWR) views, which
  would require the Oracle Diagnostic Pack license.
- Each check is isolated: one failing check logs and does not stop the rest.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List

from oracle_core.queries import (
    get_alert_log_errors,
    get_blocking_sessions,
    get_long_running_sessions,
    get_tablespace_usage,
    get_wait_events,
    get_write_workload_counters,
)

from monitor.config import get_monitor_settings
from monitor.models import DetectedIssue, IssueType, Severity

logger = logging.getLogger("monitor.checks")


async def _run_shared(function, /, *args, **kwargs):
    """Run one synchronous oracle_core query function off the event loop."""
    return await asyncio.to_thread(function, *args, **kwargs)


# ---------------------------------------------------------------------------
# 1. Tablespace usage — all containers via CDB_* views
# ---------------------------------------------------------------------------
async def check_tablespace_usage() -> List[DetectedIssue]:
    ms = get_monitor_settings()
    issues: List[DetectedIssue] = []
    for row in await _run_shared(get_tablespace_usage):
        pct = row.get("pct_used") or 0
        if pct >= ms.monitor_tablespace_crit_pct:
            severity = Severity.CRITICAL
        elif pct >= ms.monitor_tablespace_warn_pct:
            severity = Severity.WARNING
        else:
            continue
        issues.append(
            DetectedIssue(
                issue_type=IssueType.TABLESPACE_USAGE,
                severity=severity,
                summary=(
                    f"[{row['con_name']}] tablespace {row['tablespace_name']} "
                    f"is {pct}% full"
                ),
                details=row,
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 2. Blocking sessions — instance-wide from CDB root
# ---------------------------------------------------------------------------
async def check_blocking_sessions() -> List[DetectedIssue]:
    issues: List[DetectedIssue] = []
    for row in await _run_shared(get_blocking_sessions, limit=100):
        wait_s = row.get("waiting_seconds") or 0
        severity = Severity.CRITICAL if wait_s > 300 else Severity.WARNING
        issues.append(
            DetectedIssue(
                issue_type=IssueType.BLOCKING_SESSION,
                severity=severity,
                summary=(
                    f"Session {row['blocking_sid']} ({row['blocking_user']}) blocking "
                    f"session {row['waiting_sid']} ({row['waiting_user']}) "
                    f"for {wait_s}s in {row.get('con_name')}"
                ),
                details=row,
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 3. Long-running queries
# ---------------------------------------------------------------------------
async def check_long_running_queries() -> List[DetectedIssue]:
    ms = get_monitor_settings()
    issues: List[DetectedIssue] = []
    rows = await _run_shared(
        get_long_running_sessions,
        ms.monitor_long_running_query_seconds,
    )
    for row in rows:
        secs = row.get("seconds_running") or 0
        severity = (
            Severity.CRITICAL
            if secs > ms.monitor_long_running_query_seconds * 5
            else Severity.WARNING
        )
        issues.append(
            DetectedIssue(
                issue_type=IssueType.LONG_RUNNING_QUERY,
                severity=severity,
                summary=(
                    f"SID {row['sid']} ({row['username']}) running "
                    f"sql_id={row['sql_id']} for {secs}s in {row.get('con_name')}"
                ),
                details=row,
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 4. Alert log ORA- errors from the last hour (V$DIAG_ALERT_EXT)
# ---------------------------------------------------------------------------
async def check_alert_log_errors() -> List[DetectedIssue]:
    issues: List[DetectedIssue] = []
    for row in await _run_shared(get_alert_log_errors, limit=20):
        msg = str(row.get("message_text") or "")
        row["message_hash"] = hashlib.sha1(
            msg.encode("utf-8", errors="ignore")
        ).hexdigest()[:12]
        critical = "ORA-600" in msg or "ORA-7445" in msg
        issues.append(
            DetectedIssue(
                issue_type=IssueType.ALERT_LOG_ERROR,
                severity=Severity.CRITICAL if critical else Severity.WARNING,
                summary=msg[:200],
                details=row,
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 5. Top wait events (V$SYSTEM_EVENT — free, no Diagnostic Pack needed)
# ---------------------------------------------------------------------------
async def check_wait_events() -> List[DetectedIssue]:
    ms = get_monitor_settings()
    issues: List[DetectedIssue] = []
    rows = await _run_shared(get_wait_events, limit=ms.monitor_top_n_wait_events)
    for row in rows:
        issues.append(
            DetectedIssue(
                issue_type=IssueType.WAIT_EVENT,
                severity=Severity.INFO,
                summary=(
                    f"Top wait event: {row['event']} "
                    f"({row['time_waited_seconds']}s cumulative)"
                ),
                details=row,
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 6. Write workload — rates from V$SYSSTAT deltas
# ---------------------------------------------------------------------------
_WRITE_STAT_NAMES = {
    "user commits",
    "redo size",
    "execute count",
    "user calls",
}

@dataclass(frozen=True)
class WriteWorkloadSnapshot:
    """One monotonic-time sample of the cumulative V$SYSSTAT counters."""

    sampled_at: float
    values: dict[str, float]


# Process-local baseline. The first successful sample warms the baseline and
# intentionally emits no incident. A database restart/counter reset replaces
# the baseline rather than producing a false spike.
_write_workload_previous: WriteWorkloadSnapshot | None = None


def reset_write_workload_baseline() -> None:
    """Reset the process-local baseline (primarily useful for tests/restarts)."""
    global _write_workload_previous
    _write_workload_previous = None


async def check_write_workload() -> List[DetectedIssue]:
    global _write_workload_previous

    rows = await _run_shared(get_write_workload_counters)
    sampled_at = time.monotonic()
    current = {str(row["name"]): float(row.get("value") or 0) for row in rows}

    if not _WRITE_STAT_NAMES.issubset(current):
        missing = sorted(_WRITE_STAT_NAMES - current.keys())
        raise RuntimeError(f"Missing V$SYSSTAT counters: {', '.join(missing)}")

    current_snapshot = WriteWorkloadSnapshot(
        sampled_at=sampled_at,
        values=current,
    )
    previous = _write_workload_previous
    _write_workload_previous = current_snapshot
    if previous is None:
        logger.info("Write-workload baseline initialized")
        return []

    elapsed = current_snapshot.sampled_at - previous.sampled_at
    if elapsed <= 0:
        return []

    deltas = {
        name: current_snapshot.values[name] - previous.values[name]
        for name in _WRITE_STAT_NAMES
    }
    if any(value < 0 for value in deltas.values()):
        logger.info("Write-workload counters reset; baseline refreshed")
        return []

    commits_per_sec = deltas["user commits"] / elapsed
    redo_mb_per_sec = deltas["redo size"] / elapsed / 1024 / 1024
    executes_per_sec = deltas["execute count"] / elapsed
    user_calls_per_sec = deltas["user calls"] / elapsed

    ms = get_monitor_settings()
    critical_reasons = []
    warning_reasons = []

    if commits_per_sec >= ms.monitor_write_crit_commits_per_sec:
        critical_reasons.append("commits/sec")
    elif commits_per_sec >= ms.monitor_write_warn_commits_per_sec:
        warning_reasons.append("commits/sec")

    if redo_mb_per_sec >= ms.monitor_write_crit_redo_mb_per_sec:
        critical_reasons.append("redo MB/sec")
    elif redo_mb_per_sec >= ms.monitor_write_warn_redo_mb_per_sec:
        warning_reasons.append("redo MB/sec")

    if executes_per_sec >= ms.monitor_write_crit_executes_per_sec:
        critical_reasons.append("executes/sec")
    elif executes_per_sec >= ms.monitor_write_warn_executes_per_sec:
        warning_reasons.append("executes/sec")

    if critical_reasons:
        severity = Severity.CRITICAL
        trigger_metrics = critical_reasons
    elif warning_reasons:
        severity = Severity.WARNING
        trigger_metrics = warning_reasons
    else:
        return []

    details = {
        "sample_seconds": round(elapsed, 2),
        "commits_per_sec": round(commits_per_sec, 2),
        "redo_mb_per_sec": round(redo_mb_per_sec, 2),
        "executes_per_sec": round(executes_per_sec, 2),
        "user_calls_per_sec": round(user_calls_per_sec, 2),
        "trigger_metrics": trigger_metrics,
        "deltas": {name: round(value, 2) for name, value in deltas.items()},
    }

    return [
        DetectedIssue(
            issue_type=IssueType.WRITE_WORKLOAD,
            severity=severity,
            summary=(
                "High write workload detected: "
                f"{commits_per_sec:.1f} commits/sec, "
                f"{redo_mb_per_sec:.2f} redo MB/sec, "
                f"{executes_per_sec:.1f} executes/sec"
            ),
            details=details,
        )
    ]


CheckFunction = Callable[[], Awaitable[List[DetectedIssue]]]


@dataclass(frozen=True)
class CheckDefinition:
    """One monitor check and the issue type it owns."""

    issue_type: IssueType
    function: CheckFunction


@dataclass(frozen=True)
class CheckRunResult:
    """Issues plus the checks that succeeded or failed."""

    issues: List[DetectedIssue]
    successful_issue_types: set[IssueType]
    failed_issue_types: set[IssueType]


ALL_CHECKS = [
    CheckDefinition(IssueType.TABLESPACE_USAGE, check_tablespace_usage),
    CheckDefinition(IssueType.BLOCKING_SESSION, check_blocking_sessions),
    CheckDefinition(IssueType.LONG_RUNNING_QUERY, check_long_running_queries),
    CheckDefinition(IssueType.ALERT_LOG_ERROR, check_alert_log_errors),
    CheckDefinition(IssueType.WAIT_EVENT, check_wait_events),
    CheckDefinition(IssueType.WRITE_WORKLOAD, check_write_workload),
]


async def run_all_checks() -> CheckRunResult:
    """Run checks independently and report which checks succeeded."""
    all_issues: List[DetectedIssue] = []
    successful_issue_types: set[IssueType] = set()
    failed_issue_types: set[IssueType] = set()

    for definition in ALL_CHECKS:
        try:
            all_issues.extend(await definition.function())
            successful_issue_types.add(definition.issue_type)
        except Exception:
            failed_issue_types.add(definition.issue_type)
            logger.exception("Check %s failed", definition.function.__name__)

    return CheckRunResult(
        issues=all_issues,
        successful_issue_types=successful_issue_types,
        failed_issue_types=failed_issue_types,
    )
