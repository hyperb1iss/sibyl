"""Tests for client-agnostic integration content."""

from sibyl_core.integration import (
    AGENT_PROMPT_SNIPPET,
    integration_content,
    mcp_clients,
)


class TestMcpClients:
    """MCP client config builders."""

    def test_covers_expected_clients(self) -> None:
        clients = mcp_clients("http://localhost:3334/mcp")
        assert [c.id for c in clients] == ["claude", "codex", "opencode", "generic"]

    def test_every_snippet_embeds_the_mcp_url(self) -> None:
        mcp_url = "https://sibyl.example.com/mcp"
        for client in mcp_clients(mcp_url):
            assert mcp_url in client.snippet

    def test_claude_uses_the_mcp_add_command(self) -> None:
        claude = next(c for c in mcp_clients("http://localhost:3334/mcp") if c.id == "claude")
        assert claude.kind == "command"
        assert claude.snippet == "claude mcp add sibyl --transport http http://localhost:3334/mcp"

    def test_config_clients_name_a_target_file(self) -> None:
        for client in mcp_clients("http://localhost:3334/mcp"):
            if client.kind == "config":
                assert client.target


class TestIntegrationContent:
    """Full integration payload assembly."""

    def test_strips_trailing_slash_from_server_url(self) -> None:
        content = integration_content("http://localhost:3334/")
        assert content["server_url"] == "http://localhost:3334"
        assert content["mcp_url"] == "http://localhost:3334/mcp"

    def test_payload_has_all_onboarding_fields(self) -> None:
        content = integration_content("http://localhost:3334")
        assert set(content) == {
            "server_url",
            "mcp_url",
            "cli_install",
            "cli_install_alt",
            "mcp_clients",
            "prompt_snippet",
        }
        assert content["cli_install"].startswith("curl -fsSL")
        assert content["cli_install_alt"] == "brew install hyperb1iss/tap/sibyl"
        assert len(content["mcp_clients"]) == 4

    def test_mcp_clients_serialize_to_dicts(self) -> None:
        content = integration_content("http://localhost:3334")
        first = content["mcp_clients"][0]
        assert isinstance(first, dict)
        assert set(first) == {"id", "label", "kind", "language", "snippet", "target"}


class TestAgentPromptSnippet:
    """The client-agnostic agent prompt snippet."""

    def test_is_client_agnostic(self) -> None:
        # Mentions the loop and both access paths, not a single client.
        assert "recall" in AGENT_PROMPT_SNIPPET
        assert "CLI" in AGENT_PROMPT_SNIPPET
        assert "MCP" in AGENT_PROMPT_SNIPPET

    def test_does_not_mandate_a_claude_only_skill_invocation(self) -> None:
        # The snippet must not open with a Claude-specific "/sibyl" mandate.
        first_line = AGENT_PROMPT_SNIPPET.strip().splitlines()[0]
        assert "/sibyl" not in first_line
