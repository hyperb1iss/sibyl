"""MCP server construction and resource registration."""

from mcp.server import MCPServer

import sibyl.mcp_tools.context as mcp_context
from sibyl.config import settings
from sibyl.mcp_tools.registration import register_tools


def create_mcp_server() -> MCPServer:
    """Create and configure the MCP server instance.

    Returns:
        Configured MCPServer instance
    """

    auth_mode = settings.mcp_auth_mode
    jwt_secret_set = bool(settings.jwt_secret.get_secret_value())
    auth_enabled = auth_mode == "on" or (auth_mode == "auto" and jwt_secret_set)

    auth_settings = None
    auth_server_provider = None
    token_verifier = None
    if auth_enabled:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

        server_url = settings.server_url.rstrip("/")
        auth_settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=f"{server_url}/mcp",
            required_scopes=["mcp"],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["mcp"],
                default_scopes=["mcp"],
            ),
        )
        from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider

        auth_server_provider = SibylMcpOAuthProvider()
        # MCPServer does not allow configuring both an auth_server_provider
        # and a token_verifier at the same time. Our OAuth provider implements
        # access token validation via `load_access_token()`, so we rely on it.

    mcp = MCPServer(
        settings.server_name,
        auth=auth_settings,
        auth_server_provider=auth_server_provider,
        token_verifier=token_verifier,
    )

    if auth_server_provider is not None:

        @mcp.custom_route("/_oauth/login", methods=["GET"])
        async def _oauth_login_get(request):
            return await auth_server_provider.ui_login_get(request)

        @mcp.custom_route("/_oauth/login", methods=["POST"])
        async def _oauth_login_post(request):
            return await auth_server_provider.ui_login_post(request)

        @mcp.custom_route("/_oauth/org", methods=["GET"])
        async def _oauth_org_get(request):
            return await auth_server_provider.ui_org_get(request)

        @mcp.custom_route("/_oauth/org", methods=["POST"])
        async def _oauth_org_post(request):
            return await auth_server_provider.ui_org_post(request)

    register_tools(mcp)
    _register_resources(mcp)
    return mcp


def _register_resources(mcp: MCPServer) -> None:
    """Register MCP resources on the server instance."""

    # =========================================================================
    # RESOURCE: sibyl://health
    # =========================================================================

    @mcp.resource("sibyl://health")
    async def health_resource() -> str:
        """Server health and connectivity status.

        Returns JSON with:
        - status: "healthy" or "unhealthy"
        - server_name: Name of the server
        - uptime_seconds: Server uptime
        - graph_connected: Whether the active graph runtime is reachable
        - entity_counts: Count of entities by type
        - errors: Any error messages
        """
        import json

        from sibyl_core.tools.core import get_health

        # Get org context (optional for health - basic health works without org)
        org_id = await mcp_context.optional_org_id()
        health = await get_health(organization_id=org_id)
        return json.dumps(health, indent=2)

    # =========================================================================
    # RESOURCE: sibyl://stats
    # =========================================================================

    @mcp.resource("sibyl://stats")
    async def stats_resource() -> str:
        """Knowledge graph statistics.

        Returns JSON with:
        - entity_counts: Count of entities by type
        - total_entities: Total entity count
        """
        import json

        from sibyl.persistence.graph_runtime import get_graph_stats_payload

        # Get org context (required for stats)
        org_id = await mcp_context.require_org_id()
        stats = await get_graph_stats_payload(org_id)
        return json.dumps(stats, indent=2)
