# CLI Reference

The Sibyl CLI (`sibyl`) is a REST API client for interacting with your knowledge graph. Designed for
human users, assistants, and scripts, it provides rich terminal output with the SilkCircuit color
palette.

## Installation

```bash
# As a tool (recommended)
uv tool install sibyl-dev

# As a dependency
uv add sibyl-dev

# For development
cd apps/cli
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

## Command Groups

| Command                         | Description                                  |
| ------------------------------- | -------------------------------------------- |
| [`sibyl search`](./search.md)   | Semantic search across the knowledge graph   |
| [`sibyl add`](./add.md)         | Add knowledge to the graph                   |
| `sibyl capture`                 | Quick knowledge capture (content + auto-title) |
| [`sibyl archive`](./archive.md) | Browse archived raw quick captures           |
| [`sibyl session`](./session.md) | Package wake-up context for the next session |
| [`sibyl task`](./task-list.md)  | Task lifecycle management                    |
| [`sibyl project`](./project.md) | Project management                           |
| [`sibyl epic`](./epic.md)       | Epic (feature group) management              |
| [`sibyl entity`](./entity.md)   | Generic entity CRUD operations               |
| [`sibyl explore`](./explore.md) | Graph traversal and exploration              |
| [`sibyl context`](./context.md) | Manage CLI contexts                          |
| `sibyl auth`                    | Authentication and API keys                  |
| `sibyl org`                     | Organization management                      |
| `sibyl config`                  | Configuration management                     |
| `sibyl crawl`                   | Documentation source management and crawling |

## Global Options

These options are available on all commands:

```bash
sibyl --context <project_id_or_name> <command>   # Override project context
sibyl -C <project_id_or_name> <command>          # Short form
sibyl --version                                  # Show CLI version
sibyl -V                                         # Short form
```

### Output Formats

Most commands support three output formats:

| Option          | Description  | Use Case                              |
| --------------- | ------------ | ------------------------------------- |
| (default)       | Table format | Human-readable terminal output        |
| `--json` / `-j` | JSON output  | Automation, scripting, piping to `jq` |
| `--csv`         | CSV output   | Spreadsheets, data analysis           |

```bash
# Table output (default)
sibyl task list

# JSON output for scripting
sibyl task list --json | jq '.[0].name'

# CSV output
sibyl task list --csv > tasks.csv
```

## Environment Variables

| Variable             | Description                | Example                     |
| -------------------- | -------------------------- | --------------------------- |
| `SIBYL_API_URL`      | Server URL                 | `http://localhost:3334/api` |
| `SIBYL_CONTEXT`      | Override project context   | `proj_abc123`               |
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
3. Active context from config
4. Path-based project link (from current directory)

## Root Commands

### health

Check Sibyl server health:

```bash
sibyl health
```

Output:

```
sibyl is healthy
  Entities: 1234
  Relationships: 5678
```

### stats

Show knowledge graph statistics:

```bash
sibyl stats
```

### version

Show CLI version:

```bash
sibyl version
```

## Common Patterns

### AI Agent Integration

The CLI is designed for AI agent consumption with JSON-first output:

```bash
# Get task status
sibyl task show <task_id> --json | jq '.metadata.status'

# Filter and process
sibyl task list --status todo --json | jq '[.[] | {id, name, priority}]'

# Batch operations
sibyl task list --status review --json | jq -r '.[].id' | while read id; do
  sibyl task complete "$id" --learnings "Automated completion"
done
```

### Project-Scoped Operations

Link a directory to automatically scope all operations:

```bash
# Link once
cd ~/dev/my-project
sibyl project link proj_abc123

# All future commands in this directory are scoped
sibyl task list              # Only shows tasks for proj_abc123
sibyl search "auth"          # Only searches in proj_abc123
sibyl task create --title "Fix bug"  # Creates in proj_abc123
```

### Bulk Operations

Many commands support reading from stdin:

```bash
# Archive multiple tasks
sibyl task list -s todo -q "test" --json | jq -r '.[].id' | sibyl task archive --stdin --yes

# Export tasks to CSV
sibyl task list --csv > backlog.csv
```

## CLI Structure

```
sibyl
  health              Check server health
  search              Semantic search
  add                 Add knowledge
  stats               Show statistics
  version             Show version

  task                Task lifecycle management
    list              List tasks with filters
    show              Show task details
    create            Create new task
    start             Start task (doing)
    block             Block task with reason
    unblock           Resume blocked task
    review            Submit for review
    complete          Complete task
    archive           Archive task(s)
    update            Update task fields
    note              Add note to task
    notes             List task notes

  project             Project management
    list              List projects
    show              Show project details
    create            Create project
    progress          Show project progress
    link              Link directory to project
    unlink            Remove directory link
    links             List all links

  epic                Epic management
    list              List epics
    show              Show epic details
    create            Create epic
    start             Start epic
    complete          Complete epic
    archive           Archive epic
    update            Update epic
    tasks             List tasks in epic

  entity              Generic entity operations
    list              List entities by type
    show              Show entity details
    create            Create entity
    delete            Delete entity
    related           Show related entities
    history           Show entity version history

  explore             Graph traversal
    related           Find connected entities (1-hop)
    traverse          Multi-hop traversal
    dependencies      Task dependency graph
    path              Find path between entities

  context             Context management
    list              List contexts
    show              Show context details
    create            Create context
    use               Set active context
    update            Update context
    delete            Delete context
    clear             Clear active context

  auth                Authentication
    login             Log in
    logout            Log out
    status            Check auth status
    signup            Create account
    api-key           API key management

  org                 Organization
    list              List organizations
    switch            Switch organization
    current           Show current org

  config              Configuration
    show              Show config
    set               Set config value
    get               Get config value

  crawl               Web crawling
    <source_id>       Trigger crawl
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

### Authentication required

```
Authentication required
  > sibyl auth login    Log in
  > sibyl auth signup   Create account
```

Run `sibyl auth login` to authenticate.

### No project context

```
No project specified and no linked project for current directory
```

Either:

- Link the directory: `sibyl project link <project_id>`
- For task/epic commands: `--project <project_id>` or `-p`
- For search: `--context <project_id_or_name>` or `-C`
- Use global flag: `--all` or `-A` to bypass context
