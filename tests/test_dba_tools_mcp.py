"""Live smoke test for the read-only DBA MCP tools."""

import asyncio

from fastmcp import Client


MCP_SERVER_URL = "http://127.0.0.1:9000/mcp"


async def main() -> None:
    async with Client(MCP_SERVER_URL) as client:
        tools = await client.list_tools()
        tool_names = {tool.name for tool in tools}

        expected = {
            "get_active_sessions",
            "get_blocking_sessions",
            "get_top_sql",
            "get_wait_events",
            "get_tablespace_usage",
            "list_users",
        }
        missing = expected - tool_names
        assert not missing, f"Missing DBA tools: {sorted(missing)}"

        calls = [
            ("get_active_sessions", {"limit": 5}),
            ("get_blocking_sessions", {"limit": 5}),
            ("get_top_sql", {"limit": 5, "active_within_minutes": 60}),
            ("get_wait_events", {"limit": 5}),
            ("get_tablespace_usage", {"pdb_name": "ORCLPDB1"}),
            ("list_users", {"pdb_name": "ORCLPDB1"}),
        ]

        for tool_name, arguments in calls:
            print(f"\nCalling {tool_name} {arguments}")
            result = await client.call_tool(tool_name, arguments)
            print(result.data)
            assert isinstance(result.data, list), (
                f"{tool_name} did not return a list"
            )

        print("\nDBA MCP tool smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
