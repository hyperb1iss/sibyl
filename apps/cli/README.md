# sibyl-cli

Command-line interface for Sibyl. REST API client with Rich terminal output, designed for humans and AI agents.

## Quick Reference

```bash
# Install
uv tool install sibyl-cli     # or: moon run install-cli

# Configure
sibyl config set server.url http://localhost:3334/api
sibyl auth login

# Link to project (scopes all commands)
sibyl project link <project_id>
```

## Core Commands

```bash
sibyl search "query"                    # Semantic search
sibyl add "title" "content"             # Add knowledge
sibyl task list --status todo,doing     # List tasks
sibyl task start <id>                   # Start task
sibyl task complete <id> --learnings "..." # Complete with learnings
```

## All Commands

| Command | Purpose |
|---------|---------|
| `search` | Semantic search |
| `add` | Add knowledge |
| `task` | Task lifecycle (list, start, complete, block, review) |
| `project` | Project management (list, link, create) |
| `epic` | Epic management (list, start, complete, roadmap) |
| `entity` | Entity CRUD |
| `explore` | Graph navigation (related, dependencies, communities) |
| `source` | Documentation sources (list, create, crawl) |
| `document` | View crawled documents |
| `auth` | Login, logout, API keys |
| `org` | Organization switching, member management |
| `config` | Configuration |
| `context` | Multi-server context management |
| `local` | Supabase-style local dev (start, stop, logs, reset) |

## Output Formats

```bash
sibyl task list              # JSON (default, for scripts)
sibyl task list --table      # Human-friendly
sibyl task list --csv        # Spreadsheets
```

## Context System

```bash
# Override for single command
sibyl --context myproject task list
SIBYL_CONTEXT=myproject sibyl task list

# Priority: --context flag > SIBYL_CONTEXT env > active context > path link
```

## Development

```bash
moon run cli:lint         # Ruff check
moon run cli:typecheck    # Pyright
moon run cli:test         # Tests
```

## SilkCircuit Colors

Terminal output uses the SilkCircuit palette:
- `#e135ff` Electric Purple — Headers
- `#80ffea` Neon Cyan — Interactions
- `#ff6ac1` Coral — Data/IDs
- `#50fa7b` Success Green
- `#ff6363` Error Red

## Dependencies

Depends on `sibyl-core` for shared models.
