"""Chainlit action buttons: full SQL, acknowledge, and remediation approval.

Save as chat_actions.py next to chat.py.

Split out of chat.py so the chat module stays about conversation lifecycle
and this one owns everything click-driven.

The approval flow deliberately does NOT go through the agent. The MCP
server only exposes propose_remediation, which executes nothing. Execution
happens here, in this process, after a human clicks Approve. Nothing the
model can call is capable of changing the database.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import chainlit as cl

from pydantic_ai.messages import ModelMessage, ToolReturnPart

from monitor.audit import now_iso, record_remediation
from monitor.storage import acknowledge_issue
from oracle_core.remediation import execute_remediation
from oracle_core.sql_details import get_full_sql_text


# Attribution for acknowledgements and approvals. Replace with the
# authenticated username once auth is added.
ACTOR = "chat-ui"

MAX_ACTION_BUTTONS = 5


# --------------------------------------------------------------------------
# Extracting actionable items from tool results
# --------------------------------------------------------------------------


def _as_data(content: Any) -> Any:
    """Tool results arrive as objects or JSON strings depending on transport."""
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return content
    return content


def _walk_dicts(data: Any):
    """Yield every dict nested anywhere inside a tool result."""
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _walk_dicts(value)
    elif isinstance(data, (list, tuple)):
        for item in data:
            yield from _walk_dicts(item)


def collect_targets(
    messages: list[ModelMessage],
) -> tuple[list[str], list[dict], list[dict]]:
    """Pull sql_ids, monitor issues, and remediation plans out of a turn."""
    sql_ids: list[str] = []
    issues: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []

    for msg in messages:
        for part in getattr(msg, "parts", []):
            if not isinstance(part, ToolReturnPart):
                continue

            for record in _walk_dicts(_as_data(part.content)):
                # A remediation plan: produced only by propose_remediation.
                if (
                    "action" in record
                    and "statements" in record
                    and "impact" in record
                ):
                    if all(p.get("action") != record["action"] for p in plans):
                        plans.append(record)
                    continue

                sql_id = record.get("sql_id")

                if isinstance(sql_id, str) and sql_id.strip():
                    if sql_id not in sql_ids:
                        sql_ids.append(sql_id)

                issue_id = record.get("id")
                looks_like_issue = (
                    isinstance(issue_id, int)
                    and "severity" in record
                    and "issue_type" in record
                )

                if looks_like_issue and not record.get("acknowledged"):
                    if all(i["id"] != issue_id for i in issues):
                        issues.append(
                            {
                                "id": issue_id,
                                "severity": str(record.get("severity", "")),
                            }
                        )

    return (
        sql_ids[:MAX_ACTION_BUTTONS],
        issues[:MAX_ACTION_BUTTONS],
        plans[:MAX_ACTION_BUTTONS],
    )


def build_actions(sql_ids: list[str], issues: list[dict]) -> list[cl.Action]:
    """Buttons attached to the agent's own reply."""
    actions: list[cl.Action] = []

    for sql_id in sql_ids:
        actions.append(
            cl.Action(
                name="show_sql",
                payload={"sql_id": sql_id},
                label=f"Show full SQL: {sql_id}",
            )
        )

    for issue in issues:
        actions.append(
            cl.Action(
                name="ack_issue",
                payload={"issue_id": issue["id"]},
                label=f"Acknowledge #{issue['id']} ({issue['severity']})",
            )
        )

    return actions


async def send_approval_cards(plans: list[dict[str, Any]]) -> None:
    """Render one approve/reject card per proposed remediation."""
    for plan in plans:
        statements = plan.get("statements") or []
        statement_block = "\n".join(str(s) + ";" for s in statements)
        params = plan.get("params") or {}

        reversible = (
            "Reversible" if plan.get("reversible") else "**NOT reversible**"
        )

        body = (
            f"### Proposed action: `{plan.get('action')}`\n\n"
            f"{plan.get('description', '')}\n\n"
            f"**Target:** {plan.get('target', 'unknown')}  \n"
            f"**Parameters:** `{json.dumps(params, default=str)}`  \n"
            f"**{reversible}**\n\n"
            f"**What will run:**\n```sql\n{statement_block}\n```\n\n"
            f"**Impact:** {plan.get('impact', '')}\n\n"
            f"_Nothing has run yet. This executes only if you approve._"
        )

        # Known ids instead of Chainlit's random ones: public/custom.css
        # styles the buttons by id prefix, and each handler uses card_ids
        # to remove BOTH buttons, so a card cannot be approved and then
        # rejected (or vice versa).
        card = uuid.uuid4().hex
        approve_prefix = (
            "approve" if plan.get("reversible") else "approve-irreversible"
        )
        card_ids = {
            "approve_remediation": f"{approve_prefix}-{card}",
            "reject_remediation": f"reject-{card}",
        }
        payload = {"plan": plan, "card_ids": card_ids}

        await cl.Message(
            content=body,
            author="approval",
            actions=[
                cl.Action(
                    id=card_ids["approve_remediation"],
                    name="approve_remediation",
                    payload=payload,
                    label=f"Approve and run {plan.get('action')}",
                    icon="play",
                ),
                cl.Action(
                    id=card_ids["reject_remediation"],
                    name="reject_remediation",
                    payload=payload,
                    label="Reject",
                    icon="x",
                ),
            ],
        ).send()


async def _retire_card(action: cl.Action) -> None:
    """Remove every button on an approval card, not just the one clicked."""
    card_ids = action.payload.get("card_ids") or {}

    if not card_ids:
        # Cards sent before card_ids existed.
        await action.remove()
        return

    for name, action_id in card_ids.items():
        await cl.Action(
            id=action_id,
            name=name,
            payload={},
            forId=action.forId,
        ).remove()


# --------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------


@cl.action_callback("show_sql")
async def on_show_sql(action: cl.Action) -> None:
    """Fetch untruncated SQL text straight from v$sql."""
    sql_id = action.payload.get("sql_id", "")

    try:
        result = await cl.make_async(get_full_sql_text)(sql_id)
    except Exception as exc:  # noqa: BLE001
        await cl.Message(
            content=f"**Could not read SQL `{sql_id}`:** `{exc}`",
            author="system",
        ).send()
        return

    if not result.get("found"):
        await cl.Message(
            content=f"**`{sql_id}`** - {result.get('message')}",
            author="system",
        ).send()
        return

    stats = (
        f"executions **{result.get('executions')}** · "
        f"elapsed **{result.get('elapsed_seconds')}s** · "
        f"avg **{result.get('avg_elapsed_ms')}ms** · "
        f"buffer gets **{result.get('buffer_gets')}** · "
        f"plan hash **{result.get('plan_hash_value')}** · "
        f"schema **{result.get('parsing_schema_name')}**"
    )

    await cl.Message(
        content=(
            f"### Full SQL: `{sql_id}`\n\n{stats}\n\n"
            f"```sql\n{result.get('sql_fulltext')}\n```"
        ),
        author="system",
    ).send()


@cl.action_callback("ack_issue")
async def on_ack_issue(action: cl.Action) -> None:
    """Acknowledge a monitor issue in monitor.db."""
    issue_id = action.payload.get("issue_id")

    try:
        record = await acknowledge_issue(int(issue_id), ACTOR)
    except Exception as exc:  # noqa: BLE001
        await cl.Message(
            content=f"**Could not acknowledge issue #{issue_id}:** `{exc}`",
            author="system",
        ).send()
        return

    if record is None:
        await cl.Message(
            content=f"Issue #{issue_id} no longer exists.",
            author="system",
        ).send()
        return

    await action.remove()
    await cl.Message(
        content=f"Issue **#{issue_id}** acknowledged as `{ACTOR}`.",
        author="system",
    ).send()


@cl.action_callback("reject_remediation")
async def on_reject_remediation(action: cl.Action) -> None:
    """Record the refusal. Rejections are worth auditing too."""
    plan = action.payload.get("plan") or {}

    # Buttons first, so a double-click cannot record two rejections.
    await _retire_card(action)

    await record_remediation(
        action=str(plan.get("action")),
        params=plan.get("params") or {},
        target=str(plan.get("target", "")),
        statements=list(plan.get("statements") or []),
        approved_by=ACTOR,
        requested_at=now_iso(),
        outcome="REJECTED",
    )

    await cl.Message(
        content=f"Rejected `{plan.get('action')}`. Nothing was run.",
        author="system",
    ).send()


@cl.action_callback("approve_remediation")
async def on_approve_remediation(action: cl.Action) -> None:
    """Run an allowlisted action after explicit human approval."""
    plan = action.payload.get("plan") or {}
    action_name = str(plan.get("action"))
    params = plan.get("params") or {}
    requested_at = now_iso()

    # Remove the buttons first so a slow action cannot be double-clicked,
    # and so Reject cannot be clicked after it has already run.
    await _retire_card(action)

    progress = cl.Message(
        content=f"Running `{action_name}`... this can take up to 90 seconds.",
        author="system",
    )
    await progress.send()

    try:
        result = await cl.make_async(execute_remediation)(action_name, params)
    except Exception as exc:  # noqa: BLE001
        error_text = f"{type(exc).__name__}: {exc}"

        await record_remediation(
            action=action_name,
            params=params,
            target=str(plan.get("target", "")),
            statements=list(plan.get("statements") or []),
            approved_by=ACTOR,
            requested_at=requested_at,
            outcome="FAILED",
            error=error_text,
        )

        progress.content = f"**`{action_name}` failed:** `{error_text}`"
        await progress.update()
        return

    audit_id = await record_remediation(
        action=action_name,
        params=params,
        target=str(plan.get("target", "")),
        statements=list(plan.get("statements") or []),
        approved_by=ACTOR,
        requested_at=requested_at,
        outcome="APPROVED",
        result=result,
    )

    succeeded = bool(result.get("succeeded"))
    headline = (
        f"`{action_name}` completed."
        if succeeded
        else f"`{action_name}` ran, but the expected end state was not reached."
    )

    statement_lines = "\n".join(
        f"- `{r.get('status')}` {str(r.get('statement'))[:90]}"
        + (f"\n  - {r.get('error')}" if r.get("error") else "")
        for r in (result.get("statements") or [])
    )

    await progress.remove()
    await cl.Message(
        content=(
            f"### {headline}\n\n"
            f"**Statements**\n{statement_lines}\n\n"
            f"**Before:** `{json.dumps(result.get('before'), default=str)}`\n\n"
            f"**After:** `{json.dumps(result.get('after'), default=str)}`\n\n"
            f"_Audit id {audit_id}, approved by `{ACTOR}`._"
        ),
        author="system",
    ).send()
