# Sibyl CLI

Command-line interface for Sibyl. REST API client with Rich terminal output, designed for humans,
external assistants, and scripts.

## Quick Reference

```bash
# Install
uv tool install sibyl-dev     # or: moon run cli:install

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
sibyl capture "content"                 # Quick capture from the CLI
sibyl session bundle                    # Wake up with active context
sibyl task list --status todo,doing     # List tasks
sibyl task start <id>                   # Start task
sibyl task complete <id> --learnings "..." # Complete with learnings
```

## All Commands

| Command     | Purpose                                                  |
| ----------- | -------------------------------------------------------- |
| `health`    | Check API connectivity and health                        |
| `search`    | Semantic search                                          |
| `add`       | Add knowledge                                            |
| `capture`   | Quick capture from CLI arguments or stdin                |
| `stats`     | Show knowledge graph statistics                          |
| `version`   | Show CLI version information                             |
| `task`      | Task lifecycle (list, show, create, start, block, unblock, review, complete, archive, update, note) |
| `epic`      | Epic management (list, start, complete, roadmap)         |
| `project`   | Project management (list, link, create)                  |
| `archive`   | Browse archived raw captures                             |
| `session`   | Package wake-up context for a session or agent           |
| `entity`    | Entity CRUD                                              |
| `explore`   | Graph navigation (related, traverse, dependencies, path) |
| `crawl`     | Documentation sources, crawling, and graph linking       |
| `debug`     | Debug tools for development                              |
| `dev`       | Devcontainer shell and lifecycle commands                |
| `auth`      | Login, logout, API keys                                  |
| `org`       | Organization switching, member management                |
| `config`    | Configuration                                            |
| `context`   | Multi-server context management                          |
| `local`     | Manage a local Docker-based Sibyl instance               |
| `logs`      | Tail server logs                                         |
| `update`    | Update Sibyl components                                  |

## Output Formats

```bash
sibyl task list              # Table output (default)
sibyl task list --json       # JSON for scripts
sibyl task list --csv        # Spreadsheets
```

## Source Ingestion

```bash
sibyl crawl list
sibyl crawl add "https://nextjs.org/docs" --include "docs/**"
sibyl crawl ingest <source_id>
sibyl crawl documents list --source <source_id>
```

`--include` is the preferred spelling for crawl filters. `--pattern` still works for backward
compatibility.

## Capture And Archive

```bash
sibyl capture "Redis TTL mismatch caused the stale auth token bug"
sibyl archive list --surface cli
sibyl archive show <capture_id>
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

- `#e135ff` Electric Purple: Headers
- `#80ffea` Neon Cyan: Interactions
- `#ff6ac1` Coral: Data/IDs
- `#50fa7b` Success Green
- `#ff6363` Error Red

## Dependencies

Depends on `sibyl-core` for shared models.
