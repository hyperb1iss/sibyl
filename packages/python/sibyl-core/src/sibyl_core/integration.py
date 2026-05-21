"""Client-agnostic integration content for connecting Sibyl to AI agents.

Single source of truth for the onboarding surfaces. The web setup wizard, the
dashboard connect panel, and the `sibyl local setup` CLI command all render the
same install command, MCP client configs, and agent prompt snippet built here,
so the guidance stays consistent everywhere and is never Claude-specific.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# One-liner installer from the README: installs the Sibyl CLI and local daemon.
CLI_INSTALL_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh"
)
# Alternative for users who already manage local tools with Homebrew.
CLI_INSTALL_COMMAND_ALT = "brew install hyperb1iss/tap/sibyl"


AGENT_PROMPT_SNIPPET = """## Sibyl - Your Persistent Memory

Sibyl is your durable memory across sessions. It is a knowledge graph of
decisions, patterns, tasks, and learnings. Reach it through the `sibyl` CLI or
the Sibyl MCP tools, whichever your setup has.

### The memory loop: recall, act, remember, reflect

1. **Recall** working context before you act. A past session may have solved this.
   CLI: `sibyl recall "<goal>" --intent build`. MCP: the `search` and `context` tools.
2. **Act** with that context in hand.
3. **Remember** durable knowledge as you learn it: decisions, gotchas, patterns.
   The next session should not have to rediscover it.
   CLI: `sibyl remember "Title" "What matters" --kind decision`. MCP: the `remember` tool.
4. **Reflect** at clean breakpoints to distill session notes into reviewable memory.
   CLI: `sibyl reflect "<notes>" --title "<session>"`. MCP: the `reflect` tool.

### What to capture

**Always:** non-obvious solutions, gotchas, config quirks, architectural decisions.
**Skip:** trivial facts, throwaway hacks, well-documented basics.

Make each memory findable and reusable later:

- Weak: "Fixed the auth bug."
- Strong: "JWT refresh fails silently when the Redis TTL expires. The token
  service does not handle WRONGTYPE. Fix: regenerate the token on that error."

If your client supports skills (Claude Code, Codex), run `/sibyl` for the full
command reference. Otherwise `sibyl --help` covers it.
"""


@dataclass(frozen=True)
class McpClient:
    """One way to wire Sibyl into an MCP-capable agent."""

    id: str
    label: str
    kind: str  # "command" to run in a terminal, or "config" to paste into a file
    language: str  # syntax hint for rendering: bash, json, or toml
    snippet: str
    target: str | None = None  # where a "config" snippet belongs


def mcp_clients(mcp_url: str) -> list[McpClient]:
    """Build per-client MCP setup snippets for a given Sibyl MCP endpoint URL."""
    generic_config = (
        "{\n"
        '  "mcpServers": {\n'
        '    "sibyl": {\n'
        '      "type": "http",\n'
        f'      "url": "{mcp_url}"\n'
        "    }\n"
        "  }\n"
        "}"
    )
    opencode_config = (
        "{\n"
        '  "$schema": "https://opencode.ai/config.json",\n'
        '  "mcp": {\n'
        '    "sibyl": {\n'
        '      "type": "remote",\n'
        f'      "url": "{mcp_url}",\n'
        '      "enabled": true\n'
        "    }\n"
        "  }\n"
        "}"
    )
    return [
        McpClient(
            id="claude",
            label="Claude Code",
            kind="command",
            language="bash",
            snippet=f"claude mcp add sibyl --transport http {mcp_url}",
        ),
        McpClient(
            id="codex",
            label="Codex",
            kind="command",
            language="bash",
            snippet=f"codex mcp add sibyl --url {mcp_url}",
        ),
        McpClient(
            id="opencode",
            label="opencode",
            kind="config",
            language="json",
            snippet=opencode_config,
            target="opencode.json",
        ),
        McpClient(
            id="generic",
            label="Generic MCP",
            kind="config",
            language="json",
            snippet=generic_config,
            target="your client's MCP config",
        ),
    ]


def integration_content(server_url: str) -> dict:
    """Assemble the full onboarding integration payload for a Sibyl server.

    `server_url` is the public base URL of the Sibyl API; a trailing slash is
    tolerated. Returns a JSON-serializable dict consumed by the web setup
    surfaces and the CLI.
    """
    base = server_url.rstrip("/")
    mcp_url = f"{base}/mcp"
    return {
        "server_url": base,
        "mcp_url": mcp_url,
        "cli_install": CLI_INSTALL_COMMAND,
        "cli_install_alt": CLI_INSTALL_COMMAND_ALT,
        "mcp_clients": [asdict(client) for client in mcp_clients(mcp_url)],
        "prompt_snippet": AGENT_PROMPT_SNIPPET,
    }
