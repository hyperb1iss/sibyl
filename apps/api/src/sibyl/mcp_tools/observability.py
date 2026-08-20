"""Developer observability MCP tools."""

from typing import Any

from mcp.server import MCPServer

import sibyl.mcp_tools.context as mcp_context
from sibyl.persistence.auth_runtime import has_owner_membership


async def require_owner_context(ctx: mcp_context.McpContext) -> None:
    """Require OWNER membership for the current MCP context."""
    if not await has_owner_membership(org_id=ctx.org_id, user_id=ctx.user_id):
        raise ValueError("OWNER role required for log access")


def register_observability_tools(mcp: MCPServer) -> None:
    """Register owner-only observability tools."""

    @mcp.tool()
    async def logs(
        limit: int = 50,
        service: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent server logs for debugging and development.

        Returns log entries from the server's in-memory ring buffer.
        Useful for debugging issues without needing direct server access.

        Requires OWNER role (super admin equivalent).

        Args:
            limit: Maximum entries to return (default 50, max 500)
            service: Filter by service name (api, worker)
            level: Filter by log level (debug, info, warning, error)

        Returns:
            List of log entries with timestamp, service, level, event, context

        Examples:
            logs()                    # Last 50 entries
            logs(limit=100)           # Last 100 entries
            logs(service="worker")    # Worker logs only
            logs(level="error")       # Errors only
        """
        from sibyl_core.logging import LogBuffer

        # Require auth context
        ctx = await mcp_context.require_context()

        # Check OWNER role (super admin)
        await require_owner_context(ctx)

        # Clamp limit
        limit = min(max(1, limit), 500)

        # Get logs from buffer
        buffer = LogBuffer.get()
        entries = buffer.tail(n=limit, service=service, level=level)
        return [e.to_dict() for e in entries]
