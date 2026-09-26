"""Block 12 test: call the consolidated database-status MCP tool."""

import asyncio
from typing import Any

from fastmcp import Client


MCP_SERVER_URL = "http://127.0.0.1:9000/mcp"


async def main() -> None:
    client = Client(MCP_SERVER_URL)

    async with client:
        print("Connected to MCP server")

        tools = await client.list_tools()
        tool_names = {tool.name for tool in tools}

        assert "get_database_status" in tool_names, (
            "get_database_status tool was not found"
        )

        print("\nCalling get_database_status using the default PDB:")

        default_result = await client.call_tool(
            "get_database_status",
            {},
        )
        default_status = require_status_payload(
            default_result.data,
        )

        print(default_status)
        validate_status_payload(default_status)

        print("\nCalling get_database_status using ORCLPDB2:")

        pdb2_result = await client.call_tool(
            "get_database_status",
            {
                "pdb_name": "ORCLPDB2",
            },
        )
        pdb2_status = require_status_payload(
            pdb2_result.data,
        )

        print(pdb2_status)
        validate_status_payload(pdb2_status)

        assert (
            str(pdb2_status["pdb_status"]["name"]).upper()
            == "ORCLPDB2"
        ), "The requested ORCLPDB2 status was not returned"

        print("\nBlock 12 passed")


def require_status_payload(data: Any) -> dict[str, Any]:
    """Return a dictionary MCP payload or fail the test clearly."""
    assert isinstance(data, dict), (
        "get_database_status did not return a dictionary"
    )
    return data


def validate_status_payload(status: dict[str, Any]) -> None:
    """Validate the consolidated status response structure."""
    expected_sections = {
        "database_identity",
        "instance_status",
        "database_role",
        "startup",
        "pdb_status",
    }

    assert expected_sections.issubset(status), (
        "The consolidated status response is missing sections"
    )

    assert status["database_identity"].get("name"), (
        "Database name was not returned"
    )
    assert status["instance_status"].get("instance_name"), (
        "Instance name was not returned"
    )
    assert status["instance_status"].get("status"), (
        "Instance status was not returned"
    )
    assert status["database_role"].get("database_role"), (
        "Database role was not returned"
    )
    assert status["startup"].get("startup_time"), (
        "Startup time was not returned"
    )
    assert status["pdb_status"].get("name"), (
        "PDB name was not returned"
    )
    assert status["pdb_status"].get("open_mode"), (
        "PDB open mode was not returned"
    )


if __name__ == "__main__":
    asyncio.run(main())
