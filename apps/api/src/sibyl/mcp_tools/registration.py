"""Top-level registration for the public MCP tool surface."""

from mcp.server import MCPServer

from sibyl.mcp_tools.management import register_management_tools
from sibyl.mcp_tools.memory import register_memory_tools
from sibyl.mcp_tools.observability import register_observability_tools
from sibyl.mcp_tools.retrieval import register_retrieval_tools
from sibyl.mcp_tools.synthesis import register_synthesis_tools


def register_tools(mcp: MCPServer) -> None:
    """Register every public MCP tool by cohesive domain."""
    register_retrieval_tools(mcp)
    register_synthesis_tools(mcp)
    register_memory_tools(mcp)
    register_management_tools(mcp)
    register_observability_tools(mcp)
