"""Block 7 test: PydanticAI uses MCP to list Oracle PDBs."""

import asyncio

from pydantic_ai.messages import (
    ModelResponse,
    ToolCallPart,
)

from agent_app.agent import create_oracle_agent


async def main() -> None:
    agent = create_oracle_agent()

    question = (
        "Use the live database tools to list all pluggable "
        "databases and show whether each one is open."
    )

    print("User question:")
    print(question)

    async with agent:
        result = await agent.run(question)

    print("\nTool calls:")

    tool_names: list[str] = []

    for message in result.all_messages():
        if not isinstance(message, ModelResponse):
            continue

        for part in message.parts:
            if isinstance(part, ToolCallPart):
                tool_names.append(part.tool_name)

                print(f"- Tool: {part.tool_name}")
                print(f"  Arguments: {part.args}")

    print("\nFinal agent output:")
    print(result.output)

    assert "list_pdbs" in tool_names, (
        "The model did not call list_pdbs"
    )

    assert isinstance(result.output, str), (
        "The agent did not return text"
    )

    assert "ORCLPDB1" in result.output.upper(), (
        "ORCLPDB1 was missing from the final response"
    )

    assert "ORCLPDB2" in result.output.upper(), (
        "ORCLPDB2 was missing from the final response"
    )

    print("\nBlock 7 passed")


if __name__ == "__main__":
    asyncio.run(main())