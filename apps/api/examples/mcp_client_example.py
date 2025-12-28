"""Example of calling Sibyl as an MCP client.

This shows how to connect to a running Sibyl MCP server
and call tools programmatically.

Prerequisites:
  1. Start Sibyl server: uv run sibyl serve
  2. Run this script: uv run python examples/mcp_client_example.py
"""

import asyncio
import json

import httpx


async def call_mcp_tool(
    tool_name: str,
    arguments: dict,
    base_url: str = "http://localhost:3334",
) -> dict:
    """Call an MCP tool on the Sibyl server.

    Args:
        tool_name: Name of the tool (search, explore, add, manage)
        arguments: Tool arguments as a dict
        base_url: Server base URL

    Returns:
        Tool result as a dict
    """
    async with httpx.AsyncClient() as client:
        # MCP uses JSON-RPC 2.0
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        response = await client.post(
            f"{base_url}/mcp",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        result = response.json()

        if "error" in result:
            raise Exception(f"MCP error: {result['error']}")

        return result.get("result", {})


async def main():
    """Demonstrate MCP client usage."""

    print("=" * 60)
    print("SIBYL MCP CLIENT EXAMPLE")
    print("=" * 60)
    print("\nConnecting to http://localhost:3334/mcp")
    print("Make sure Sibyl server is running: uv run sibyl serve\n")

    try:
        # -----------------------------------------------------------------
        # Search for patterns
        # -----------------------------------------------------------------
        print("1. Calling search tool...")
        result = await call_mcp_tool(
            "search",
            {
                "query": "error handling best practices",
                "types": ["pattern", "rule"],
                "limit": 5,
            },
        )
        print(f"   Results: {json.dumps(result, indent=2)[:200]}...")

        # -----------------------------------------------------------------
        # Explore graph structure
        # -----------------------------------------------------------------
        print("\n2. Calling explore tool...")
        result = await call_mcp_tool(
            "explore",
            {
                "mode": "list",
                "types": ["pattern"],
                "limit": 3,
            },
        )
        print(f"   Found {result.get('total', 0)} patterns")

        # -----------------------------------------------------------------
        # Add knowledge
        # -----------------------------------------------------------------
        print("\n3. Calling add tool...")
        result = await call_mcp_tool(
            "add",
            {
                "title": "MCP client example learning",
                "content": "Successfully called Sibyl via MCP protocol",
                "entity_type": "episode",
                "category": "examples",
            },
        )
        print(f"   Result: {result.get('message', 'unknown')}")

        # -----------------------------------------------------------------
        # Health check
        # -----------------------------------------------------------------
        print("\n4. Calling manage tool (health)...")
        result = await call_mcp_tool(
            "manage",
            {"action": "health"},
        )
        print(f"   Status: {result.get('data', {}).get('status', 'unknown')}")

        print("\n" + "=" * 60)
        print("MCP CLIENT EXAMPLE COMPLETE")
        print("=" * 60)

    except httpx.ConnectError:
        print("ERROR: Could not connect to Sibyl server")
        print("Start it with: uv run sibyl serve")
    except Exception as e:
        print(f"ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(main())
