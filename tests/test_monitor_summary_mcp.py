"""Block 13 test: call the monitor-summary MCP tool."""

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

        assert "get_monitor_summary" in tool_names, (
            "get_monitor_summary tool was not found"
        )

        print("\nCalling get_monitor_summary:")

        result = await client.call_tool(
            "get_monitor_summary",
            {
                "include_acknowledged": True,
                "limit": 5,
            },
        )
        summary = require_summary_payload(result.data)

        print(summary)
        validate_summary_payload(summary)

        print("\nBlock 13 passed")


def require_summary_payload(data: Any) -> dict[str, Any]:
    """Return a dictionary MCP payload or fail the test clearly."""
    assert isinstance(data, dict), (
        "get_monitor_summary did not return a dictionary"
    )
    return data


def validate_summary_payload(summary: dict[str, Any]) -> None:
    """Validate the monitor-summary response structure."""
    expected_fields = {
        "generated_at",
        "data_status",
        "current_definition",
        "current_window_seconds",
        "latest_observation_at",
        "total_stored_issue_count",
        "current_issue_count",
        "current_unacknowledged_count",
        "current_acknowledged_count",
        "severity_counts",
        "issue_type_counts",
        "most_serious_issue",
        "issues",
        "returned_issue_count",
    }

    assert expected_fields.issubset(summary), (
        "The monitor summary is missing expected fields"
    )
    assert summary["data_status"] in {
        "EMPTY",
        "CURRENT",
        "CLEAR",
    }
    assert isinstance(summary["issues"], list)
    assert isinstance(summary["severity_counts"], dict)
    assert summary["current_window_seconds"] >= 1
    assert summary["returned_issue_count"] <= 5

    current_count = summary["current_issue_count"]
    acknowledged_count = summary["current_acknowledged_count"]
    unacknowledged_count = summary["current_unacknowledged_count"]

    assert current_count == acknowledged_count + unacknowledged_count

    most_serious = summary["most_serious_issue"]
    if current_count == 0:
        assert most_serious is None
    else:
        assert isinstance(most_serious, dict)
        assert most_serious.get("severity") in {
            "INFO",
            "WARNING",
            "CRITICAL",
        }
        assert most_serious.get("summary")


if __name__ == "__main__":
    asyncio.run(main())
