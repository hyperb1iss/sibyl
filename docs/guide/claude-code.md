---
title: Claude Code Integration
description: Using Sibyl with Claude Code via MCP
---

# Claude Code Integration

Sibyl is designed as an MCP (Model Context Protocol) server that integrates directly with Claude Code. This guide explains how to set up and use Sibyl as your AI agent's persistent memory.

## What is MCP?

The Model Context Protocol (MCP) allows AI assistants like Claude to interact with external tools and data sources. Sibyl exposes 4 MCP tools:

| Tool | Purpose |
|------|---------|
| `search` | Find knowledge by meaning |
| `explore` | Navigate graph structure |
| `add` | Create knowledge entries |
| `manage` | Task workflow and admin |

## Configuration

### HTTP Mode (Recommended)

Add to your Claude Code MCP configuration:

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

This connects to a running Sibyl server.

### Subprocess Mode

Run Sibyl as a subprocess:

```json
{
  "mcpServers": {
    "sibyl": {
      "command": "uv",
      "args": ["--directory", "/path/to/sibyl/apps/api", "run", "sibyl-serve", "-t", "stdio"],
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
sibyl auth api-key create --name "Claude Code" --scopes mcp
```

### Without Authentication (Dev Mode)

Disable auth for local development:

```bash
SIBYL_MCP_AUTH_MODE=off
```

::: warning Production Use
Always enable authentication in production environments.
:::

## The Agent Workflow

### Research -> Do -> Reflect

```
1. SEARCH FIRST     -> Search for existing knowledge
2. CHECK TASKS      -> Find active work
3. WORK & CAPTURE   -> Create knowledge as you learn
4. COMPLETE         -> Capture learnings on completion
```

### Before Implementing

```python
# Search for relevant patterns
search("what you're building")
search("common issues with OAuth")
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

## Skills Integration

### What are Skills?

Skills are Claude Code's knowledge injection system. Sibyl provides skills that teach Claude how to use the knowledge graph effectively.

### Installing Skills

```bash
moon run install-skills
```

This installs skills to `~/.claude/skills/`.

### Using Skills

Invoke skills via slash commands:

```
/sibyl
```

The skill teaches Claude:
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

1. Restart Claude Code
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

- [Skills Development](./skills.md) - Create custom skills
- [MCP Configuration](./mcp-configuration.md) - Advanced configuration
- [Agent Collaboration](./agent-collaboration.md) - Multi-agent patterns
