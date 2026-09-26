"""Block 5 test: PydanticAI uses MCP to query Oracle."""

import asyncio
from typing import Any

from pydantic_ai.messages import (
    ModelResponse,
    ToolCallPart,
)

from agent_app.agent import create_oracle_agent
from agent_app.config import get_agent_settings


async def main() -> None:
    settings = get_agent_settings()
    agent = create_oracle_agent()

    print("Model:", settings.llm_model)
    print("MCP server:", settings.mcp_server_url)

    question = (
        "Use the available database tool to determine the complete "
        "Oracle database version running in ORCLPDB2. "
        "Include the exact version number in your answer."
    )

    print("\nUser question:")
    print(question)

    async with agent:
        result = await agent.run(question)

    print("\nTool calls:")

    tool_calls: list[ToolCallPart] = []

    for message in result.all_messages():
        if not isinstance(message, ModelResponse):
            continue

        for part in message.parts:
            if isinstance(part, ToolCallPart):
                tool_calls.append(part)
                print(f"- Tool: {part.tool_name}")
                print(f"  Arguments: {part.args}")

    print("\nFinal agent output:")
    print(result.output)

    assert tool_calls, (
        "The model did not call any MCP tool"
    )

    assert any(
        tool_call.tool_name == "check_db_version"
        for tool_call in tool_calls
    ), "The model did not call check_db_version"

    assert any(
        tool_call.tool_name == "check_db_version"
        and contains_orclpdb2(tool_call.args)
        for tool_call in tool_calls
    ), "check_db_version was not called for ORCLPDB2"

    assert isinstance(result.output, str), (
        "The agent did not return a text response"
    )

    assert result.output.strip(), (
        "The agent returned an empty response"
    )

    assert "12.2.0.1.0" in result.output, (
        "The final response did not contain the exact Oracle version"
    )

    print("\nBlock 5 passed")


def contains_orclpdb2(arguments: Any) -> bool:
    """Return True when tool arguments contain ORCLPDB2."""
    return "ORCLPDB2" in str(arguments).upper()


if __name__ == "__main__":
    asyncio.run(main())
