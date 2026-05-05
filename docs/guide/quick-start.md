---
title: Quick Start
description: Get up and running with Sibyl in 5 minutes
---

# Quick Start

This guide gets you from zero to a working Sibyl setup in about 5 minutes.

## Prerequisites

Make sure you have:

- Python 3.13+ installed
- Docker (for local SurrealDB)
- An OpenAI API key

## Step 1: Install and Configure

```bash
# Clone and install
git clone https://github.com/hyperb1iss/sibyl.git
cd sibyl
./setup-dev.sh

# Configure
cp .env.example .env
```

Edit `.env` and set:

```bash
SIBYL_OPENAI_API_KEY=sk-your-openai-key
SIBYL_JWT_SECRET=any-secret-string-for-development
SIBYL_STORE=surreal
SIBYL_COORDINATION_BACKEND=local
```

## Step 2: Start the Server

```bash
# Start the recommended local-dev stack
moon run dev

# Or just the API
cd apps/api && uv run sibyld serve
```

The server is now running on `http://localhost:3334`.

`moon run dev` starts local SurrealDB on port `8000` and keeps jobs plus schedules in-process under
the API server. If you want Redis-backed coordination later, set `SIBYL_COORDINATION_BACKEND=redis`
and start Redis explicitly with Docker Compose.

## Step 3: Configure the CLI

```bash
# Set the server URL
sibyl config set server.url http://localhost:3334/api

# Check health
sibyl health
```

## Step 4: Create Your First Entity

Let's add some knowledge to the graph:

```bash
# Add a learning
sibyl add "Python async gotcha" "Always use asyncio.gather() for concurrent awaits, not sequential awaits in a loop"
```

You should see:

```
Added: Python async gotcha (id: episode_abc123)
```

## Step 5: Search for Knowledge

```bash
# Search by meaning
sibyl search "async concurrency"
```

The search will find your learning even though you searched for different words - that's semantic
search in action.

## Step 6: Create a Task

Tasks require a project, so let's create one:

```bash
# Create a project
sibyl project create --name "My First Project" --description "Learning Sibyl"

# Note the project ID from the output, then create a task
sibyl task create --title "Try Sibyl features" --project <project_id>
```

## Step 7: Manage Task Lifecycle

```bash
# List your tasks
sibyl task list --status todo

# Start working on a task
sibyl task start <task_id>

# Check what's in progress
sibyl task list --status doing

# Complete with learnings
sibyl task complete <task_id> --learnings "Sibyl CLI is intuitive!"
```

## Step 8: Link a Directory (Optional)

If you're working on a specific project, link your directory:

```bash
# In your project directory
cd ~/my-project

# Link to a Sibyl project
sibyl project link proj_abc123

# Now task commands auto-scope to this project
sibyl task list --status todo  # Shows only tasks for linked project
```

## Step 9: Explore the Graph

```bash
# List all projects
sibyl entity list --type project

# Find related entities
sibyl explore related entity_xyz

# See task dependencies
sibyl explore dependencies task_abc
```

## Using with Claude Code

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

Now Claude can:

- Search your knowledge graph
- Track tasks
- Capture learnings
- Navigate relationships

## The Agent Workflow

When working with Claude Code and Sibyl:

```
1. SEARCH FIRST     -> sibyl search "topic"
2. CHECK TASKS      -> sibyl task list --status doing
3. WORK & CAPTURE   -> sibyl add "learning" "description"
4. COMPLETE         -> sibyl task complete --learnings "..."
```

## Common Commands Reference

| Action           | Command                                      |
| ---------------- | -------------------------------------------- |
| Search knowledge | `sibyl search "query"`                       |
| Add a learning   | `sibyl add "title" "content"`                |
| List tasks       | `sibyl task list --status todo`              |
| Start a task     | `sibyl task start <id>`                      |
| Complete a task  | `sibyl task complete <id> --learnings "..."` |
| List projects    | `sibyl project list`                         |
| Link directory   | `sibyl project link <id>`                    |
| Check health     | `sibyl health`                               |

## Output Formats

The CLI supports multiple output formats:

```bash
# Table (default, human-readable)
sibyl task list

# JSON (for scripting and agents)
sibyl task list --json

# CSV (for spreadsheets)
sibyl task list --csv
```

## What's Next?

Now that you have Sibyl running:

1. **Read the Philosophy** - [Introduction](./index.md) explains the "search, work, capture" mindset
2. **Understand the Graph** - [Knowledge Graph](./knowledge-graph.md) explains how entities connect
3. **Set Up Claude** - [Claude Code Integration](./claude-code.md) for full AI agent support
4. **Learn Entity Types** - [Entity Types](./entity-types.md) to know what to capture

## Tips for Success

::: tip Search First Before implementing anything, search the graph. Patterns, past solutions, and
gotchas might already be there. :::

::: tip Capture Non-Obvious Learnings If it took time to figure out, it's worth saving. Future you
(or your AI agent) will thank you. :::

::: tip Use Project Context Link your directories to projects. It keeps task lists focused and
prevents cross-project confusion. :::

::: warning Don't Skip Learnings The `--learnings` flag on task completion is where the real value
accumulates. Be specific about what you learned. :::
