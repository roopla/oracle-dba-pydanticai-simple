"""Background poller with incident lifecycle and recommendation reuse."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from monitor.config import get_monitor_settings
from monitor.models import DetectedIssue, IssueRecord, IssueStatus, Severity
from monitor.storage import (
    get_issue_by_fingerprint,
    record_issue_observation,
    resolve_missing_issues,
)

logger = logging.getLogger("monitor.poller")


async def _run_all_checks():
    """Lazy import keeps storage/API unit tests independent of Oracle drivers."""
    from monitor.checks import run_all_checks

    return await run_all_checks()


async def _collect_diagnostics(issue: DetectedIssue):
    """Collect approved evidence only when a fresh recommendation is needed."""
    from monitor.diagnostics import collect_diagnostics

    return await collect_diagnostics(issue)


async def _generate_recommendation(issue: DetectedIssue):
    """Lazy import keeps lifecycle tests independent of the configured LLM."""
    from monitor.llm import get_recommendation

    return await get_recommendation(issue)


def _severity_rank(severity: Severity) -> int:
    return {
        Severity.INFO: 1,
        Severity.WARNING: 2,
        Severity.CRITICAL: 3,
    }[severity]


def _deduplicate_issues(issues: list[DetectedIssue]) -> list[DetectedIssue]:
    """Keep one observation per fingerprint for one poll cycle."""
    by_fingerprint: dict[str, DetectedIssue] = {}

    for issue in issues:
        fingerprint = issue.fingerprint()
        existing = by_fingerprint.get(fingerprint)
        if existing is None or _severity_rank(issue.severity) > _severity_rank(
            existing.severity
        ):
            by_fingerprint[fingerprint] = issue

    return list(by_fingerprint.values())


def _recommendation_reason(
    existing: IssueRecord | None,
    issue: DetectedIssue,
) -> str | None:
    """Return why the LLM should run, or None to reuse the stored result."""
    if existing is None:
        return "new"
    if existing.status == IssueStatus.RESOLVED:
        return "reopened"
    if existing.severity != issue.severity:
        return "severity_changed"
    return None


async def run_poll_cycle() -> int:
    """Run checks, persist detections, then resolve successfully cleared checks."""
    check_result = await _run_all_checks()
    issues = _deduplicate_issues(check_result.issues)

    # Include all detected fingerprints even if recommendation/storage work
    # later fails. A detected condition must never be resolved in that poll.
    active_fingerprints = {issue.fingerprint() for issue in issues}

    for issue in issues:
        fingerprint = issue.fingerprint()
        try:
            existing = await get_issue_by_fingerprint(fingerprint)
            recommendation_reason = _recommendation_reason(existing, issue)
            recommendation = None

            if recommendation_reason is not None:
                logger.info(
                    "Collecting approved diagnostics for %s (%s)",
                    fingerprint,
                    recommendation_reason,
                )
                try:
                    issue.diagnostics = await _collect_diagnostics(issue)
                except Exception as exc:
                    logger.exception(
                        "Diagnostic collection failed for %s",
                        fingerprint,
                    )
                    issue.diagnostics = {
                        "diagnostic_error": str(exc)[:200],
                    }

                logger.info(
                    "Generating recommendation for %s (%s)",
                    fingerprint,
                    recommendation_reason,
                )
                recommendation = await _generate_recommendation(issue)

            await record_issue_observation(issue, recommendation)
        except Exception:
            logger.exception("Failed to process issue: %s", issue.summary)

    resolved_count = await resolve_missing_issues(
        active_fingerprints=active_fingerprints,
        successful_issue_types=check_result.successful_issue_types,
        resolved_at=datetime.now(timezone.utc),
    )

    logger.info(
        "Poll cycle lifecycle update: detected=%d resolved=%d failed_checks=%d",
        len(active_fingerprints),
        resolved_count,
        len(check_result.failed_issue_types),
    )
    return len(active_fingerprints)


async def poll_forever(stop_event: asyncio.Event) -> None:
    interval = get_monitor_settings().monitor_poll_interval_seconds
    logger.info("Monitor poller started (interval=%ss)", interval)

    while not stop_event.is_set():
        try:
            count = await run_poll_cycle()
            logger.info("Poll cycle complete: %d detected issue(s)", count)
        except Exception:
            logger.exception("Poll cycle failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("Monitor poller stopped")
