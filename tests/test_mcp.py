"""Block 3 and Block 7 test: discover and call Oracle MCP tools."""

import asyncio

from fastmcp import Client


MCP_SERVER_URL = "http://127.0.0.1:9000/mcp"


async def main() -> None:
    client = Client(MCP_SERVER_URL)

    async with client:
        print("Connected to MCP server")

        tools = await client.list_tools()

        print("\nAvailable tools:")
        for tool in tools:
            print(f"- {tool.name}")
            print(f"  Description: {tool.description}")

        tool_names = {
            tool.name
            for tool in tools
        }

        assert "check_db_version" in tool_names, (
            "check_db_version tool was not found"
        )

        assert "list_pdbs" in tool_names, (
            "list_pdbs tool was not found"
        )

        print("\nCalling check_db_version using the default PDB:")

        default_result = await client.call_tool(
            "check_db_version",
            {},
        )

        print(default_result.data)

        assert default_result.data, (
            "Default database version call returned no data"
        )

        print("\nCalling check_db_version using ORCLPDB2:")

        pdb2_result = await client.call_tool(
            "check_db_version",
            {
                "pdb_name": "ORCLPDB2",
            },
        )

        print(pdb2_result.data)

        assert pdb2_result.data, (
            "ORCLPDB2 database version call returned no data"
        )

        print("\nCalling list_pdbs:")

        pdb_result = await client.call_tool(
            "list_pdbs",
            {},
        )

        print(pdb_result.data)

        assert pdb_result.data, (
            "list_pdbs returned no data"
        )

        pdb_names = {
            row["name"]
            for row in pdb_result.data
        }

        assert "ORCLPDB1" in pdb_names, (
            "ORCLPDB1 was not returned"
        )

        assert "ORCLPDB2" in pdb_names, (
            "ORCLPDB2 was not returned"
        )

        print("\nBlock 7 MCP tool test passed")


if __name__ == "__main__":
    asyncio.run(main())