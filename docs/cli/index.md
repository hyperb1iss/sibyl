# CLI Reference

The Sibyl CLI (`sibyl`) is a REST client for your knowledge graph and memory loop. It is built for
human users, AI agents, and scripts, with rich terminal output in the SilkCircuit palette and
JSON-first output for automation.

`sibyl` is the client. The server daemon is `sibyld` (serve, worker, db, migrate). This reference
covers the client.

## Installation

```bash
# User install
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh

# Remote-only install
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh -s -- --remote

# For development
moon run cli:install-dev
```

## Quick Start

```bash
# Authenticate (creates or uses the active context)
sibyl auth login

# Link the current directory to a project
sibyl project link <project_id>

# Now commands auto-scope to that project
sibyl task list --status todo
sibyl search "authentication"

# Recall a working context pack for a goal
sibyl recall "wire up the password reset endpoint"
```

## Command Families

The CLI has roughly three dozen command groups. They fall into five families.

### Memory loop

Capture knowledge and recall it back into agent context.

| Command                           | Description                                         |
| --------------------------------- | --------------------------------------------------- |
| [`sibyl recall`](./recall.md)     | Recall a compact working context pack for an agent  |
| [`sibyl remember`](./remember.md) | Remember a decision, plan, idea, claim, or learning |
| [`sibyl reflect`](./reflect.md)   | Reflect raw notes into reviewable memory candidates |
| [`sibyl capture`](./capture.md)   | Quick capture with an auto-derived title            |
| [`sibyl note`](./remember.md)     | Add a task note or capture a free note memory       |
| [`sibyl add`](./add.md)           | Add knowledge with explicit title and content       |
| [`sibyl search`](./search.md)     | Semantic search across graph and crawled docs       |
| [`sibyl show`](./show.md)         | Show a graph entity or raw memory by ID             |
| [`sibyl entity`](./entity.md)     | Generic entity CRUD operations                      |
| [`sibyl explore`](./explore.md)   | Graph traversal and exploration                     |
| [`sibyl archive`](./archive.md)   | Browse raw quick captures                           |
| [`sibyl session`](./session.md)   | Package a wake-up context bundle                    |
| [`sibyl context`](./context.md)   | Compile context packs and manage CLI contexts       |

### Work tracking

Plan and run tasks, epics, and projects.

| Command                         | Description                     |
| ------------------------------- | ------------------------------- |
| [`sibyl task`](./task-list.md)  | Task lifecycle management       |
| [`sibyl epic`](./epic.md)       | Epic (feature group) management |
| [`sibyl project`](./project.md) | Project management              |

### Sources and synthesis

Ingest external docs and produce source-grounded artifacts.

| Command                             | Description                                   |
| ----------------------------------- | --------------------------------------------- |
| [`sibyl crawl`](./crawl.md)         | Web crawling and documentation ingestion      |
| [`sibyl synthesis`](./synthesis.md) | Source-grounded synthesis (plan/draft/verify) |

### Memory governance

Review, promote, share, and audit memory.

| Command                                       | Description                                 |
| --------------------------------------------- | ------------------------------------------- |
| [`sibyl memory-audit`](./memory.md)           | Inspect memory audit receipts               |
| [`sibyl memory-inspect`](./memory.md)         | Inspect a memory source and its audit trail |
| [`sibyl memory-promote`](./memory.md)         | Preview or auto-review candidate promotion  |
| [`sibyl memory-share`](./memory.md)           | Preview memory sharing across scopes        |
| [`sibyl memory-space`](./memory.md)           | Memory-space inspection and preview         |
| [`sibyl memory-review`](./memory.md)          | Reflection review queue and dream-cycle     |
| [`sibyl pending-writes`](./pending-writes.md) | Inspect and replay locally buffered writes  |

### System

Auth, organizations, configuration, and operations.

| Command                   | Description                                       |
| ------------------------- | ------------------------------------------------- |
| [`sibyl auth`](./auth.md) | Authentication, tokens, and API keys              |
| `sibyl login`             | Log in to the active or provided server           |
| `sibyl logout`            | Clear stored auth credentials                     |
| `sibyl whoami`            | Check auth status for the active context          |
| [`sibyl org`](./org.md)   | Organizations and member management               |
| `sibyl context`           | Server/org/project context bundles (see above)    |
| `sibyl config`            | Manage CLI configuration                          |
| `sibyl health`            | Check Sibyl server health                         |
| `sibyl stats`             | Show knowledge graph statistics                   |
| `sibyl version`           | Show version information                          |
| `sibyl logs`              | View server logs (requires OWNER role)            |
| `sibyl debug`             | Debug tools for development (requires OWNER role) |
| `sibyl up`                | Start the local server and web UI                 |
| `sibyl down`              | Stop the local server and web UI                  |
| `sibyl serve`             | Start the local embedded daemon                   |
| `sibyl start`             | Start the local embedded daemon in the background |
| `sibyl stop`              | Stop the background local daemon                  |
| `sibyl service`           | Install native local daemon service files         |
| `sibyl docker`            | Manage a self-hosted Docker deployment            |
| `sibyl local`             | Legacy local Docker stack commands                |
| `sibyl dev`               | Devcontainer shell and lifecycle commands         |
| `sibyl update`            | Update Sibyl components                           |
| `sibyl skill`             | Install the loader skill and print bundled packs  |

## Global Options

These options are available on the root command:

```bash
sibyl --context <project_id_or_name> <command>   # Override project context
sibyl -C <project_id_or_name> <command>          # Short form
sibyl --version                                  # Show CLI version
sibyl -V                                         # Short form
```

### Output Formats

Most commands support a `--json` / `-j` flag for machine-readable output, and list-style commands
add `--csv`:

| Option          | Description  | Use Case                              |
| --------------- | ------------ | ------------------------------------- |
| (default)       | Table format | Human-readable terminal output        |
| `--json` / `-j` | JSON output  | Automation, scripting, piping to `jq` |
| `--csv`         | CSV output   | Spreadsheets, data analysis           |

```bash
sibyl task list                              # Table (default)
sibyl task list --json | jq '.[0].name'      # JSON for scripting
sibyl task list --csv > tasks.csv            # CSV export
```

## Environment Variables

| Variable             | Description                | Example                     |
| -------------------- | -------------------------- | --------------------------- |
| `SIBYL_CONTEXT`      | Override project context   | `proj_abc123`               |
| `SIBYL_API_URL`      | Server URL (legacy)        | `http://localhost:3334/api` |
| `SIBYL_ACCESS_TOKEN` | Auth token (rarely needed) | `eyJhbG...`                 |

## Configuration

### Config File Location

```
~/.sibyl/config.toml
```

### Config Structure

```toml
[server]
url = "http://localhost:3334/api"

[paths]
"/home/user/project-a" = "proj_abc123"
"/home/user/project-b" = "proj_xyz789"

[context]
active = "local"

[contexts.local]
server_url = "http://localhost:3334"
org_slug = ""
default_project = ""

[contexts.prod]
server_url = "https://sibyl.example.com"
org_slug = "myorg"
default_project = "proj_main"
```

### Context Priority

When resolving project context, the CLI checks in this order:

1. `--context` / `-C` flag (highest priority)
2. `SIBYL_CONTEXT` environment variable
3. Active context's default project
4. Path-based project link (from the current directory)

## Common Patterns

### AI Agent Integration

The CLI is built for AI agent consumption with JSON-first output:

```bash
# Recall a context pack and pull titles
sibyl recall "implement OAuth2" --json | jq '.items[].title'

# Get task status
sibyl task show <task_id> --json | jq '.metadata.status'

# Filter and process
sibyl task list --status todo --json | jq '[.[] | {id, name, priority}]'
```

### The Memory Loop

```bash
# Recall before starting work
sibyl recall "fix the auth token refresh bug" --intent debug

# Capture findings as you go
sibyl capture "Redis WRONGTYPE on refresh was the root cause"

# Reflect a session into reviewable candidates
cat session-notes.md | sibyl reflect --persist --review
```

### Project-Scoped Operations

```bash
# Link once
cd ~/dev/my-project
sibyl project link proj_abc123

# All future commands in this directory are scoped
sibyl task list                      # Only proj_abc123 tasks
sibyl search "auth"                  # Only searches proj_abc123
sibyl task create --title "Fix bug"  # Creates in proj_abc123
```

### Bulk Operations

```bash
# Archive done tasks via stdin
sibyl task list -s done --json | jq -r '.[].id' | sibyl task archive --stdin --yes

# Export tasks to CSV
sibyl task list --csv > backlog.csv
```

## SilkCircuit Colors

The CLI uses the SilkCircuit palette for terminal output:

| Color           | Hex       | Usage                |
| --------------- | --------- | -------------------- |
| Electric Purple | `#e135ff` | Headers, importance  |
| Neon Cyan       | `#80ffea` | Interactions, paths  |
| Coral           | `#ff6ac1` | Data, IDs, secondary |
| Electric Yellow | `#f1fa8c` | Warnings             |
| Success Green   | `#50fa7b` | Success states       |
| Error Red       | `#ff6363` | Errors               |

## Troubleshooting

### Cannot connect to server

```
Cannot connect to Sibyl server
  > Check that the Sibyl server is running
```

Ensure the server is running:

```bash
sibyld serve  # or: moon run dev
```

Writes attempted while offline are buffered locally. Inspect and replay them with
[`sibyl pending-writes`](./pending-writes.md).

### Authentication required

```
Authentication required
  > sibyl auth login    Log in
```

Run [`sibyl auth login`](./auth.md) to authenticate.

### No project context

```
No project specified and no linked project for current directory
```

Either:

- Link the directory: `sibyl project link <project_id>`
- For task/epic commands: pass `--project <project_id>` or `-p`
- For search and recall: pass `--all` or `-a`
- Use the global flag `--context` / `-C` to override
