# Sibyl CLI

Command-line interface for Sibyl. A REST API client with Rich terminal output, designed for humans,
external assistants, and scripts. The package is published as `sibyl-dev`; the executable is
`sibyl`.

## Quick Reference

```bash
# Install
uv tool install sibyl-dev     # or: moon run cli:install

# Configure
sibyl init --remote https://sibyl.example.com
sibyl login

# Link to project (scopes all commands)
sibyl project link <project_id>
```

## The Memory Loop

```bash
sibyl recall "goal"                          # Agent-ready context before work
sibyl remember "title" "content" --kind decision  # Capture durable memory
sibyl reflect "raw notes" --persist          # Distill notes into candidates
sibyl capture "content"                      # Fast verbatim capture
sibyl search "query"                         # Semantic search
sibyl session bundle                         # Wake up with active context
```

## Task Workflow

```bash
sibyl task list --status todo,doing          # List tasks
sibyl task start <id>                        # Start a task
sibyl task complete <id> --learnings "..."   # Complete with learnings
```

## All Commands

### Memory loop

| Command    | Purpose                                                                |
| ---------- | ---------------------------------------------------------------------- |
| `recall`   | Compile an agent-ready Markdown or JSON context pack before work       |
| `remember` | Capture decisions, plans, ideas, claims, artifacts, and session memory |
| `reflect`  | Distill raw notes into reviewable memory candidates                    |
| `search`   | Semantic search across graph memory and crawled docs                   |
| `add`      | Add structured knowledge                                               |
| `capture`  | Fast verbatim capture from arguments or stdin                          |
| `note`     | Add a task note or capture a free note memory                          |
| `session`  | Package wake-up context for a session or agent                         |

### Work tracking

| Command   | Purpose                                                                                                    |
| --------- | ---------------------------------------------------------------------------------------------------------- |
| `task`    | Task lifecycle (list, show, create, start, block, unblock, review, complete, archive, update, note, notes) |
| `epic`    | Epic management (list, show, create, start, complete, archive, update, roadmap, tasks)                     |
| `project` | Projects and directory linking (list, show, create, progress, link, relink, unlink, links)                 |
| `entity`  | Generic entity CRUD and bi-temporal history                                                                |
| `explore` | Graph navigation (related, traverse, dependencies, path)                                                   |
| `stats`   | Knowledge graph statistics                                                                                 |

### Sources & synthesis

| Command     | Purpose                                                           |
| ----------- | ----------------------------------------------------------------- |
| `crawl`     | Documentation sources, crawling, document browsing, graph linking |
| `synthesis` | Source-grounded synthesis (plan, draft, verify, remember)         |
| `archive`   | Browse archived raw quick captures                                |

### Memory governance

| Command          | Purpose                                                   |
| ---------------- | --------------------------------------------------------- |
| `memory-audit`   | Inspect memory audit receipts                             |
| `memory-inspect` | Inspect a memory source and its audit trail               |
| `memory-promote` | Preview or auto-review reflection candidate promotion     |
| `memory-share`   | Preview memory sharing before enabling share writes       |
| `memory-space`   | Memory-space inspection and agent-recall preview          |
| `memory-review`  | Reflection review queue automation (drain, dream, status) |

### System

| Command          | Purpose                                           |
| ---------------- | ------------------------------------------------- |
| `health`         | Check API connectivity and health                 |
| `auth`           | Login, logout, tokens, API keys                   |
| `org`            | Organization switching and member management      |
| `context`        | Multi-server context bundles                      |
| `config`         | CLI configuration                                 |
| `serve` / `stop` | Start or stop the local embedded daemon           |
| `docker`         | Manage a self-hosted Docker deployment            |
| `local`          | Legacy local Docker stack commands                |
| `pending-writes` | Inspect and replay locally buffered writes        |
| `logs`           | Tail server logs (requires OWNER role)            |
| `debug`          | Debug tools for development (requires OWNER role) |
| `dev`            | Devcontainer shell and lifecycle commands         |
| `skill`          | Print or install the canonical Sibyl skill        |
| `update`         | Update Sibyl components                           |
| `version`        | Show CLI version information                      |

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

## Capturing Memory

```bash
sibyl recall "ship the SurrealDB-native memory path" --intent build
sibyl capture "Redis TTL mismatch caused the stale auth token bug"
sibyl remember "Token TTL decision" \
  "Keep refresh token TTL longer than access token TTL." --kind decision --domain auth
sibyl remember "Worker routing decision" \
  "Verifier agents run after non-trivial patches." --kind decision --task task_abc
echo "Raw planning notes..." | sibyl reflect --title "Planning session" --persist --review --task task_abc
sibyl archive list
sibyl archive show <capture_id>
```

In a linked project, `sibyl remember` also links to the single active `doing` task when exactly one
exists. Use `--task` for explicit links or `--no-active-task` to capture project memory without a
task edge. Persisted `sibyl reflect` output follows the same task-linking rules for its raw session
source and extracted candidates.

`sibyl reflect` accepts either an argument or stdin. With `--persist --review`, Sibyl stores the raw
notes and extracted candidates in the review queue with source IDs, confidence, extraction metadata,
and suggested memory scope. Without `--review`, persisted reflection follows the server's active
write mode. Add `--no-source` when the raw transcript is too noisy or sensitive but the extracted
candidates should still be saved.

## Context System

```bash
# Override for a single command
sibyl --context myproject task list
SIBYL_CONTEXT=myproject sibyl task list

# Priority: --context flag > SIBYL_CONTEXT env > active context > path link
```

## Development

```bash
moon run cli:lint         # Ruff check
moon run cli:typecheck    # ty
moon run cli:test         # Tests
```

## SilkCircuit Colors

Terminal output uses the SilkCircuit palette:

- `#e135ff` Electric Purple: headers
- `#80ffea` Neon Cyan: interactions
- `#ff6ac1` Coral: data and IDs
- `#50fa7b` Success Green
- `#ff6363` Error Red

## Dependencies

Depends on `sibyl-core` for shared models.
