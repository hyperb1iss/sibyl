# sibyl-cli

Command-line interface for the Sibyl knowledge graph. A REST API client with Rich terminal output,
designed for both human users and AI agents.

## Overview

sibyl-cli provides a standalone CLI that communicates with a Sibyl server via REST API. No direct
database access is required — it's purely a client application.

**Key features:**

- **JSON-first output** — Clean JSON by default for AI agent consumption
- **Human-friendly modes** — `--table` and `--csv` output formats
- **Project context** — Automatically scopes operations to the current project
- **Rich terminal output** — SilkCircuit color palette with styled tables and panels

## Installation

```bash
# As a tool (recommended)
uv tool install sibyl-cli

# As a dependency
uv add sibyl-cli

# For development
cd packages/python/sibyl-cli
uv sync
```

## Quick Start

```bash
# Configure server URL
sibyl config set server.url http://localhost:3334/api

# Authenticate
sibyl auth login

# Link current directory to a project
sibyl project link <project_id>

# Now all commands are scoped to that project
sibyl task list --status todo
sibyl search "authentication"
```

## Commands

### Core Commands

```bash
sibyl health              # Check server health
sibyl search "query"      # Semantic search
sibyl add "title" "content"  # Add knowledge
sibyl stats               # Show statistics
sibyl version             # Show version
sibyl context             # Show current project context
```

### Task Management

```bash
# List tasks
sibyl task list                          # All tasks in project
sibyl task list --status todo            # Filter by status
sibyl task list --status todo,doing      # Multiple statuses
sibyl task list --priority high          # Filter by priority
sibyl task list --all                    # Bypass project context

# Task lifecycle
sibyl task start <id>                    # Move to "doing"
sibyl task complete <id>                 # Move to "done"
sibyl task complete <id> --learnings "..." # With learnings
sibyl task block <id> --reason "..."     # Move to "blocked"
sibyl task unblock <id>                  # Move back to "doing"
sibyl task review <id>                   # Move to "review"

# Direct updates
sibyl task update <id> --status done     # Set status directly
sibyl task update <id> --priority high   # Set priority
sibyl task update <id> --name "New name" # Rename

# Create tasks
sibyl task create "Task name" "Description" --priority high

# Get task details
sibyl task get <id>
sibyl task get <id> --table              # Human-readable
```

### Project Management

```bash
# List projects
sibyl project list
sibyl project list --table

# Link directory to project
sibyl project link <project_id>

# Unlink directory
sibyl project unlink

# Create project
sibyl project create "Project Name" "Description"

# Get project details
sibyl project get <id>
sibyl project get <id> --table
```

### Epic Management

```bash
# List epics
sibyl epic list
sibyl epic list --project <project_id>
sibyl epic list --status in_progress

# Create epic
sibyl epic create "Epic Name" "Description" --project <project_id>

# Get epic details
sibyl epic get <id>
```

### Entity Operations

```bash
# List entities
sibyl entity list
sibyl entity list --type pattern
sibyl entity list --type pattern,episode

# Get entity
sibyl entity get <id>
sibyl entity get <id> --table

# Delete entity
sibyl entity delete <id>
```

### Graph Exploration

```bash
# List by type
sibyl explore list --types project

# Find related entities
sibyl explore related <entity_id>

# View dependencies
sibyl explore dependencies <task_id>

# View communities
sibyl explore communities
```

### Source & Crawling

```bash
# List sources
sibyl source list

# Create source
sibyl source create "React Docs" https://react.dev --depth 3

# Trigger crawl
sibyl crawl <source_id>
sibyl crawl <source_id> --depth 2
```

### Authentication

```bash
# Login
sibyl auth login

# Check status
sibyl auth status

# Logout
sibyl auth logout

# API keys
sibyl auth api-key list
sibyl auth api-key create --name "CI/CD" --scopes mcp,api:read
sibyl auth api-key revoke <key_id>
```

### Organization

```bash
# List organizations
sibyl org list

# Switch organization
sibyl org switch <org_id>

# Current organization
sibyl org current
```

### Configuration

```bash
# Show config
sibyl config show

# Set values
sibyl config set server.url http://localhost:3334/api

# Get value
sibyl config get server.url
```

### Context Management

```bash
# Show current context
sibyl context

# Show with tree view
sibyl context -t

# Override context for single command
sibyl --context <project_id> task list
# Or with env var
SIBYL_CONTEXT=<project_id> sibyl task list
```

## Output Formats

### JSON (Default)

```bash
sibyl task list
# Returns: [{"id": "task_abc", "name": "...", ...}, ...]
```

### Table

```bash
sibyl task list --table
# Returns formatted table with SilkCircuit colors
```

### CSV

```bash
sibyl task list --csv
# Returns: id,name,status,priority,...
```

## Configuration

### Config File

Located at `~/.sibyl/config.toml`:

```toml
[server]
url = "http://localhost:3334/api"

[paths]
"/home/user/project-a" = "proj_abc123"
"/home/user/project-b" = "proj_xyz789"

[context]
active = "proj_abc123"
```

### Environment Variables

```bash
SIBYL_API_URL=http://localhost:3334/api   # Server URL
SIBYL_CONTEXT=proj_abc123                  # Override project context
SIBYL_ACCESS_TOKEN=...                     # Auth token (rarely needed)
```

### Context Priority

1. `--context` / `-C` flag (highest)
2. `SIBYL_CONTEXT` environment variable
3. Active context from config
4. Path-based project link (from current directory)

## Structure

```
src/sibyl_cli/
├── __init__.py        # Package entry, main()
├── main.py            # Typer app, command registration
├── client.py          # REST API client
├── common.py          # Colors, output helpers
│
├── auth.py            # auth subcommand
├── auth_store.py      # Token storage
├── config_cmd.py      # config subcommand
├── config_store.py    # Config file handling
├── context.py         # context subcommand
├── state.py           # Session state
│
├── task.py            # task subcommand
├── project.py         # project subcommand
├── epic.py            # epic subcommand
├── entity.py          # entity subcommand
├── explore.py         # explore subcommand
├── source.py          # source subcommand
├── crawl.py           # crawl subcommand
├── org.py             # org subcommand
└── onboarding.py      # First-run experience
```

## Development

### moonrepo Tasks

```bash
moon run cli:lint         # Ruff check
moon run cli:format       # Ruff format
moon run cli:typecheck    # Pyright
moon run cli:check        # All of the above
```

### Direct Commands

```bash
cd packages/python/sibyl-cli

uv run ruff check src/
uv run ruff format src/
uv run pyright src/
```

## AI Agent Integration

The CLI is designed for AI agent consumption:

```bash
# JSON output by default
sibyl search "authentication" | jq '.[0].name'

# Specific fields
sibyl task get <id> | jq '.status'

# Filter and process
sibyl task list --status todo | jq '[.[] | {id, name, priority}]'
```

### Claude Code / MCP Integration

The CLI works alongside the MCP server:

- **MCP tools** — Real-time, in-session operations
- **CLI** — Bulk operations, scripting, CI/CD

Example workflow:

```bash
# Script: Close all completed tasks
sibyl task list --status review | jq -r '.[].id' | while read id; do
  sibyl task complete "$id" --learnings "Automated completion"
done
```

## SilkCircuit Colors

The CLI uses the SilkCircuit palette for terminal output:

```python
from sibyl_cli.common import (
    ELECTRIC_PURPLE,  # "#e135ff" - Headers, importance
    NEON_CYAN,        # "#80ffea" - Interactions
    CORAL,            # "#ff6ac1" - Data, IDs
    ELECTRIC_YELLOW,  # "#f1fa8c" - Warnings
    SUCCESS_GREEN,    # "#50fa7b" - Success
    ERROR_RED,        # "#ff6363" - Errors
)
```

## Comparison: CLI vs Server CLI

| Feature          | sibyl-cli (this)  | apps/api CLI               |
| ---------------- | ----------------- | -------------------------- |
| Communication    | REST API          | Direct database            |
| Install location | Anywhere          | Server machine             |
| Auth             | Token-based       | Local env vars             |
| Use case         | Client operations | Server administration      |
| Commands         | User-facing CRUD  | db backup, generate, serve |

## Dependencies

- `typer` — CLI framework
- `rich` — Terminal formatting
- `httpx` — HTTP client
- `pyjwt` — Token handling
- `pyyaml` / `tomli-w` — Config files
- `sibyl-core` — Shared models
