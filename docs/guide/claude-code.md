---
title: Agents & MCP
description: Connect Sibyl to any AI agent over MCP
---

# Agents & MCP

Sibyl runs as an MCP (Model Context Protocol) server, so any MCP-capable AI agent can use it as
persistent memory. This guide explains how to connect an agent and what Sibyl exposes once it is
wired in. It works the same for coding agents like Claude Code, Codex, and opencode, and for
personal assistants like OpenClaw.

## What is MCP?

The Model Context Protocol (MCP) is an open standard that lets AI agents interact with external
tools and data sources. Sibyl exposes 11 MCP tools:

| Tool               | Purpose                                                        |
| ------------------ | -------------------------------------------------------------- |
| `search`           | Unified semantic search across the graph and crawled docs      |
| `context`          | Compile a working context pack (intent, wake/recall/deep)      |
| `synthesis_plan`   | Plan source-grounded synthesis from authorized memory          |
| `synthesis_draft`  | Draft, verify, and optionally remember a synthesis artifact    |
| `synthesis_verify` | Verify citation, freshness, redaction, and gap coverage        |
| `explore`          | Navigate the graph: list, related, traverse, dependencies      |
| `add`              | Create knowledge entries (episodes, patterns, tasks, projects) |
| `remember`         | Capture durable memory (decision, plan, idea, claim, ...)      |
| `reflect`          | Reflect raw notes into reviewable memory candidates            |
| `manage`           | State changes: task workflow, crawl, sync, analysis, admin     |
| `logs`             | Recent server logs (OWNER role)                                |

The four memory-loop tools (`context`, `remember`, `reflect`, and the `synthesis_*` family)
implement the [memory loop](./memory-loop.md) for agents. `search`, `explore`, `add`, and `manage`
cover retrieval and workflow.

## Connecting an Agent

### HTTP Mode (Recommended)

Point your agent at a running Sibyl server. The exact step depends on the client:

```bash
# Claude Code
claude mcp add sibyl --transport http http://localhost:3334/mcp

# Codex
codex mcp add sibyl --url http://localhost:3334/mcp
```

Clients that use a JSON config file take the standard MCP server block:

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp"
    }
  }
}
```

The Sibyl web UI generates the exact command for your server in the Connect panel. See
[MCP Configuration](./mcp-configuration.md) for transport, auth, and per-client detail.

### Subprocess Mode

Run Sibyl as a subprocess:

```json
{
  "mcpServers": {
    "sibyl": {
      "command": "uv",
      "args": ["--directory", "/path/to/sibyl/apps/api", "run", "sibyld", "serve", "-t", "stdio"],
      "env": {
        "SIBYL_OPENAI_API_KEY": "sk-...",
        "SIBYL_JWT_SECRET": "your-secret"
      }
    }
  }
}
```

Use subprocess mode when:

- Running locally without a server
- Each project needs isolated state
- CI/CD environments

## Authentication

### With Authentication

When `SIBYL_JWT_SECRET` is set, MCP requires authentication:

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

Create an API key:

```bash
sibyl auth api-key create --name "my-agent" --scopes mcp
```

### Without Authentication (Dev Mode)

Disable auth for local development:

```bash
SIBYL_MCP_AUTH_MODE=off
```

::: warning Production Use Always enable authentication in production environments. :::

## The Agent Workflow

### The Memory Loop

```
1. RECALL           -> Pull working context with `context`
2. ACT              -> Start a task, do the work
3. REMEMBER         -> Capture decisions and learnings
4. REFLECT          -> Distill session notes into candidates
```

See [The Memory Loop](./memory-loop.md) for the cycle in full.

### Before Implementing

```python
# Recall a working context pack for the goal
context(goal="implement OAuth refresh", intent="build")

# Or search directly for relevant patterns
search("what you're building")
search("error handling patterns", types=["pattern"])
```

### During Implementation

```python
# Check for related work
explore(mode="list", types=["task"], project="proj_abc", status="doing")

# Track progress via notes
manage("update_task", entity_id="task_xyz",
       data={"notes": "Implemented OAuth callback"})
```

### After Completing

```python
# Complete with learnings
manage("complete_task", entity_id="task_xyz",
       data={"learnings": "OAuth redirect URIs must match exactly..."})

# Or add standalone knowledge
add("OAuth redirect insight",
    "Redirect URIs must match exactly including trailing slashes...",
    category="authentication")
```

## Tool Reference

### search

Find entities by semantic meaning:

```python
search(
    query="OAuth implementation patterns",
    types=["pattern", "episode"],    # Filter by type
    language="python",               # Filter by language
    status="todo",                   # Filter tasks by status
    project="proj_abc",              # Scope to project
    limit=10                         # Max results
)
```

### context

Compile a working context pack for a goal. This is the recall step of the memory loop:

```python
context(
    goal="implement OAuth refresh",
    intent="build",       # build, plan, ideate, research, review, debug, decide, learn
    layer="recall",       # wake (fast), recall (default), deep_search (wide)
    project="proj_abc"    # Scope recall to a project
)
```

### remember

Capture durable, typed memory:

```python
remember(
    title="Chose SurrealDB for the runtime",
    content="One engine replaces three backends...",
    kind="decision"       # decision, plan, idea, claim, episode, session, ...
)
```

### reflect

Distill raw notes into reviewable memory candidates:

```python
reflect(
    content="Long session notes to distill...",
    persist=True,         # Persist candidates into the graph
    review=True           # Route to the review queue instead of direct promotion
)
```

### explore

Navigate the graph structure:

```python
# List entities
explore(mode="list", types=["project"])
explore(mode="list", types=["task"], project="proj_abc", status="todo")

# Find related entities
explore(mode="related", entity_id="pattern_abc")

# Task dependencies
explore(mode="dependencies", entity_id="task_xyz")

# Multi-hop traversal
explore(mode="traverse", entity_id="proj_abc", depth=2)
```

### add

Create new knowledge:

```python
# Add a learning (default type: episode)
add(
    title="Redis connection insight",
    content="Pool size must be >= concurrent requests...",
    category="database",
    languages=["python", "redis"]
)

# Create a task
add(
    title="Implement OAuth",
    content="Add OAuth2 login flow",
    entity_type="task",
    project="proj_abc",
    priority="high"
)

# Create a pattern
add(
    title="Retry with backoff",
    content="Implementation pattern...",
    entity_type="pattern",
    languages=["python"]
)
```

### manage

Handle state changes:

```python
# Task workflow
manage("start_task", entity_id="task_xyz")
manage("complete_task", entity_id="task_xyz", data={"learnings": "..."})
manage("block_task", entity_id="task_xyz", data={"reason": "..."})

# Admin
manage("health")
manage("stats")

# Crawling
manage("crawl", data={"url": "https://docs.example.com", "depth": 3})
```

### synthesis_plan / synthesis_draft / synthesis_verify

Draft verified documents grounded in your own memory:

```python
# Plan a section outline from authorized memory
synthesis_plan(goal="How our auth system handles token refresh")

# Draft and verify an artifact
synthesis_draft(goal="Release runbook for sibyld")

# Verify citation, freshness, redaction, and gap coverage
synthesis_verify(goal="How our auth system handles token refresh")
```

See [Synthesis](./synthesis.md) for the three-stage flow in detail.

## Skills Integration

### What are Skills?

Skills are a knowledge-injection mechanism in agents like Claude Code and Codex. Sibyl ships a skill
that teaches the agent how to use the knowledge graph effectively.

### Installing Skills

Use the Connect page in the Sibyl web UI for the current MCP config and prompt snippet. The CLI
helper remains available for advanced local setups, but it should be an explicit choice rather than
part of install.

The helper installs the Sibyl skill and hooks for Claude Code, and the skill for Codex.

### Using Skills

In a client that supports skills, invoke it as a slash command:

```
/sibyl
```

The skill teaches the agent:

- CLI commands
- Workflow patterns
- Best practices
- Common pitfalls

### Skill Files

Skills are defined in `skills/` directory:

```
skills/
└── sibyl/
    ├── SKILL.md
    ├── WORKFLOWS.md
    └── EXAMPLES.md
```

## Agent Patterns

### Starting a Session

```python
# 1. Check current context
explore(mode="list", types=["project"])

# 2. Find in-progress work
search("", types=["task"], status="doing")

# 3. Or find next todo
search("", types=["task"], status="todo", project="proj_abc")

# 4. Start working
manage("start_task", entity_id="task_xyz")
```

### Research Pattern

```python
# Before implementing, search for:
# 1. Existing patterns
search("what you're building", types=["pattern"])

# 2. Past learnings
search("related topic", types=["episode"])

# 3. Known issues
search("common mistakes with X")

# 4. Team rules
explore(mode="list", types=["rule"])
```

### Knowledge Capture Pattern

```python
# When you discover something non-obvious
add(
    title="Descriptive title",
    content="What, why, how, caveats...",
    category="domain",
    languages=["relevant", "languages"]
)
```

### Task Completion Pattern

```python
# Always complete with learnings
manage(
    action="complete_task",
    entity_id="task_xyz",
    data={
        "learnings": """
        Key insight: OAuth redirect URIs must match exactly.

        Problem: Google OAuth was silently failing
        Cause: Trailing slash mismatch in redirect URI
        Fix: Ensure production and config URIs match exactly
        """
    }
)
```

## Best Practices

### 1. Search Before Implementing

Always check if relevant knowledge exists:

```python
search("what you're about to build")
```

### 2. Work in Task Context

Don't do significant work without a task:

```python
# Find or create a task first
explore(mode="list", types=["task"], project="proj_abc", status="todo")
manage("start_task", entity_id="task_xyz")
```

### 3. Capture Non-Obvious Learnings

If it took time to figure out, save it:

```python
add("Descriptive title", "Detailed content with why and how")
```

### 4. Use Project Context

Scope task operations to projects:

```python
explore(mode="list", types=["task"], project="proj_abc", status="todo")
```

### 5. Complete with Learnings

The `learnings` field is where value accumulates:

```python
manage("complete_task", entity_id="task_xyz",
       data={"learnings": "Specific, actionable insight"})
```

## Troubleshooting

### Connection Failed

1. Check server is running: `curl http://localhost:3334/api/health`
2. Verify URL in MCP config
3. Check firewall/network settings

### Authentication Errors

1. Verify API key is valid
2. Check key has `mcp` scope
3. Ensure `Authorization` header is set

### No Results from Search

1. Check organization context
2. Verify entity types exist
3. Try broader search terms

### Tools Not Available

1. Restart your agent
2. Check MCP server logs
3. Verify config syntax

## Example Session

A complete session might look like:

```python
# 1. Start session - check context
explore(mode="list", types=["project"])
# Returns: proj_auth, proj_api, proj_web

# 2. Find my in-progress work
search("", types=["task"], status="doing", project="proj_auth")
# Returns: task_oauth (OAuth implementation)

# 3. Search for relevant patterns
search("OAuth callback handling", types=["pattern"])
# Returns: pattern_oauth_callback

# 4. Work on implementation...

# 5. Capture a discovery
add(
    title="OAuth state parameter must be cryptographic",
    content="Using predictable state parameters enables CSRF attacks...",
    category="security",
    languages=["python"]
)

# 6. Complete task
manage("complete_task", entity_id="task_oauth",
       data={"learnings": "OAuth state must be cryptographic random..."})
```

## Next Steps

- [The Memory Loop](./memory-loop.md) - The cycle agents follow
- [Skills Development](./skills.md) - Create custom skills
- [MCP Configuration](./mcp-configuration.md) - Advanced configuration
- [Agent Collaboration](./agent-collaboration.md) - Shared-assistant patterns
