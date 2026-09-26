---
title: MCP Setup
description: Connecting Cursor, Claude Code, and Claude Desktop to Sibyl
---

# MCP Setup

Sibyl exposes an HTTP MCP endpoint at `/mcp`. Most agents do not need it: an agent that can run
shell commands uses the `sibyl` CLI, and [`sibyl setup`](../cli/setup.md) signs the CLI in and
installs the skill that teaches it. Use this page for clients that only speak MCP, such as Cursor or
Claude Desktop, with a scoped API key that has the `mcp` scope.

## Create The Key

```bash
sibyl auth api-key create --name "mcp-client" --scopes mcp
```

Use a project or memory-space restriction when the client should only see a specific workspace:

```bash
sibyl auth api-key create --name "project-agent" \
  --scopes mcp \
  --projects proj_abc123
```

## Shared HTTP Configuration

Most MCP clients accept the same HTTP shape. A local `sibyl up` install lives at
`http://localhost:3334/mcp`; on a remote or shared server, swap in your own host:

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp",
      "headers": {
        "Authorization": "Bearer sk_live_replace_me"
      }
    }
  }
}
```

Keep the key out of committed files. Prefer your client's secret store or a local ignored config
file.

## Cursor

Add Sibyl as an HTTP MCP server in Cursor's MCP settings:

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp",
      "headers": {
        "Authorization": "Bearer sk_live_replace_me"
      }
    }
  }
}
```

Restart the MCP server from Cursor's settings after editing the config.

## Claude Code

Use Claude Code's MCP configuration with the same HTTP server entry:

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp",
      "headers": {
        "Authorization": "Bearer sk_live_replace_me"
      }
    }
  }
}
```

Add a short project instruction so agents actually use the memory loop:

```md
Search Sibyl before non-trivial work, capture durable gotchas after solving them, and keep task
state current when work spans multiple steps.
```

## Claude Desktop

Claude Desktop can use the same HTTP MCP server entry in its MCP server config:

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp",
      "headers": {
        "Authorization": "Bearer sk_live_replace_me"
      }
    }
  }
}
```

Restart Claude Desktop after changing the MCP config.

## Verify

A connected client sees 13 Sibyl tools plus the `sibyl://health` and `sibyl://stats` resources. Ask
the client to search Sibyl for a known memory. If the request fails:

- Confirm the API key starts with `sk_live_` or `sk_test_`.
- Confirm the key has the `mcp` scope.
- Confirm the client can reach `http://localhost:3334/mcp`.
- Check Settings, Security, API Keys to make sure the key was not revoked.
