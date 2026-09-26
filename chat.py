"""ChatGPT-style web interface for the Oracle DBA agent.

Standalone:

    uv run --env-file .env.otel --env-file .env.mcp --env-file .env.agent \
      chainlit run chat.py -w --port 8001

Or mounted alongside the monitor dashboard via main.py.

This module owns the conversation lifecycle. Everything click-driven -
full SQL, acknowledgements, remediation approval - lives in chat_actions.py.
The monitor link comes from [[UI.header_links]] in .chainlit/config.toml.
"""

from __future__ import annotations

import chainlit as cl
import httpx

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, RetryPromptPart, ToolReturnPart

from agent_app.agent import create_oracle_agent
from agent_app.config import get_agent_settings
from agent_app.model_catalog import fetch_available_model_ids

# Importing this module registers its @cl.action_callback handlers.
from chat_actions import build_actions, collect_targets, send_approval_cards


_settings = get_agent_settings()

# One agent per model, built lazily and reused across sessions.
_agents: dict[str, Agent] = {}

# Model list from OpenWebUI. Cached only on success, so a transient
# failure at startup does not kill the picker for the whole process.
_model_choices: list[str] | None = None


def _get_model_choices() -> list[str]:
    """Configured default first, then whatever OpenWebUI exposes."""
    global _model_choices

    if _model_choices is not None:
        return _model_choices

    try:
        discovered = fetch_available_model_ids()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        print(f"Warning: could not load OpenWebUI model list: {exc}")
        return [_settings.llm_model]

    ordered = [_settings.llm_model, *discovered]
    _model_choices = list(dict.fromkeys(m.strip() for m in ordered if m.strip()))
    return _model_choices


def _get_agent(model_name: str) -> Agent:
    if model_name not in _agents:
        _agents[model_name] = create_oracle_agent(model_name)
    return _agents[model_name]


@cl.set_chat_profiles
async def chat_profiles() -> list[cl.ChatProfile]:
    """Model picker shown at the top of the chat."""
    return [
        cl.ChatProfile(
            name=model_id,
            markdown_description=(
                f"**{model_id}**"
                + ("  _(default)_" if model_id == _settings.llm_model else "")
            ),
        )
        for model_id in _get_model_choices()
    ]


@cl.set_starters
async def starters() -> list[cl.Starter]:
    return [
        cl.Starter(
            label="Standby health",
            message="Is my Data Guard standby healthy?",
        ),
        cl.Starter(
            label="Tablespace usage",
            message="Show me tablespace usage and flag anything above 85%.",
        ),
        cl.Starter(
            label="Recent slowdown",
            message="What was the database waiting on in the last 30 minutes?",
        ),
        cl.Starter(
            label="Top SQL",
            message="What are the top SQL statements by elapsed time?",
        ),
    ]


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("history", [])

    selected = cl.user_session.get("chat_profile") or _settings.llm_model
    cl.user_session.set("model", selected)

    await cl.Message(
        content=(
            "**Oracle DBA agent** is ready.\n\n"
            f"Model: `{selected}`\n\n"
            "Ask about PDBs, tablespace usage, sessions, blocking, top SQL, "
            "wait events, ASH activity, or Data Guard."
        ),
        author="system",
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    history: list[ModelMessage] = cl.user_session.get("history") or []
    model_name: str = cl.user_session.get("model") or _settings.llm_model
    agent = _get_agent(model_name)

    reply = cl.Message(content="")
    await reply.send()

    new_messages: list[ModelMessage] = []

    try:
        # Opens the MCP toolset connection for the duration of this turn.
        async with agent:
            async with agent.run_stream(
                message.content,
                message_history=history,
            ) as result:
                async for delta in result.stream_text(delta=True):
                    await reply.stream_token(delta)

                new_messages = result.new_messages()
                cl.user_session.set("history", result.all_messages())

        # Surface what the tools actually did, instead of letting the
        # model paper over a failure with an apology.
        for msg in new_messages:
            for part in getattr(msg, "parts", []):
                if isinstance(part, RetryPromptPart):
                    await cl.Message(
                        content=f"**Tool error**\n```\n{part.content}\n```",
                        author="debug",
                    ).send()
                elif isinstance(part, ToolReturnPart):
                    async with cl.Step(name=part.tool_name, type="tool") as step:
                        step.output = str(part.content)[:2000]

        sql_ids, issues, plans = collect_targets(new_messages)

        actions = build_actions(sql_ids, issues)

        if actions:
            reply.actions = actions

    except Exception as exc:  # noqa: BLE001
        reply.content = f"**Error:** `{type(exc).__name__}: {exc}`"
        await reply.update()
        return

    await reply.update()

    # Approval cards go in their own messages, after the reply is settled.
    if plans:
        await send_approval_cards(plans)
