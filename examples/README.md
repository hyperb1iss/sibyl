# Sibyl Examples

Example scripts demonstrating Sibyl usage patterns.

## Quick Start

```bash
# Start dependencies
docker compose up -d

# Start Sibyl server
uv run sibyl serve
```

## Examples

### [quickstart.py](quickstart.py)

Basic usage of all 4 tools (search, explore, add, manage).

```bash
uv run python examples/quickstart.py
```

### [task_workflow_example.py](task_workflow_example.py)

Full task management lifecycle including:
- Creating projects and tasks
- Task state transitions (start → block → unblock → review → complete)
- Automatic knowledge linking
- Learning capture and effort estimation

```bash
uv run python examples/task_workflow_example.py
```

### [mcp_client_example.py](mcp_client_example.py)

Calling Sibyl as an MCP client over HTTP.

```bash
# First start the server
uv run sibyl serve

# Then run the client
uv run python examples/mcp_client_example.py
```

## Tool Reference

| Tool | Purpose |
|------|---------|
| `search` | Semantic discovery across all entity types |
| `explore` | Graph navigation (list, related, traverse) |
| `add` | Create entities with auto-linking |
| `manage` | Workflows, crawling, admin operations |

## Entity Types

| Type | Description |
|------|-------------|
| `episode` | Temporal knowledge (learnings, insights) |
| `pattern` | Coding patterns and best practices |
| `rule` | Sacred rules and invariants |
| `template` | Code templates and boilerplates |
| `task` | Work items with workflow states |
| `project` | Container for related tasks |
| `source` | Knowledge source (URL, file) |
| `document` | Crawled/ingested content |
