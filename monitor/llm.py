"""Recommendation agent for the monitor.

The model interprets detected facts plus approved diagnostic evidence. It does
not generate SQL, PL/SQL, RMAN, shell commands, or other executable commands.
All database diagnostics are owned by reviewed application code.
"""

from __future__ import annotations

import json
import logging

from pydantic_ai import Agent, PromptedOutput

from agent_app.model import create_model
from monitor.config import get_monitor_settings
from monitor.models import DetectedIssue, Recommendation, fallback_recommendation

logger = logging.getLogger("monitor.agent")

SYSTEM_PROMPT = (
    "You are a senior Oracle DBA analyzing one detected database incident. "
    "You will receive measured incident details and, when available, results "
    "from approved read-only diagnostic queries. "
    "Use only the supplied evidence. Do not invent Oracle view names, columns, "
    "sessions, SQL IDs, objects, metrics, configuration, or causes. "
    "Do not generate SQL, PL/SQL, RMAN commands, shell commands, or any other "
    "executable command. The application owns all diagnostic SQL. "
    "If evidence is insufficient, clearly say what cannot yet be concluded. "
    "Keep root_cause and recommended_action concise and operationally useful. "
    "confidence MUST be a decimal number from 0.0 through 1.0; for example, "
    "0.78 means 78 percent confidence. Never return 78 or 95 as confidence."
)

_agent: Agent | None = None


def get_recommendation_agent() -> Agent:
    """Lazily create the recommendation agent."""
    global _agent

    if _agent is None:
        monitor_settings = get_monitor_settings()
        model = create_model(monitor_settings.monitor_llm_model or None)
        _agent = Agent(
            model=model,
            name="oracle_monitor_recommendation_agent",
            output_type=PromptedOutput(Recommendation),
            instructions=SYSTEM_PROMPT,
        )

    return _agent


async def get_recommendation(issue: DetectedIssue) -> Recommendation:
    prompt = (
        f"Issue type: {issue.issue_type.value}\n"
        f"Severity: {issue.severity.value}\n"
        f"Summary: {issue.summary}\n"
        "Measured details:\n"
        f"{json.dumps(issue.details, indent=2, default=str)}\n\n"
        "Approved diagnostic evidence:\n"
        f"{json.dumps(issue.diagnostics, indent=2, default=str)}\n"
    )
    try:
        result = await get_recommendation_agent().run(prompt)
        return result.output
    except Exception as exc:
        logger.exception("Recommendation agent failed for issue: %s", issue.summary)
        return fallback_recommendation(str(exc)[:200])
