# Sibyl Examples

Example scripts demonstrating Sibyl usage patterns.

## Quick Start

Run these from the repository root.

```bash
# Start local data services and the Sibyl server daemon
sibyld up
```

`moon run dev` does the same thing plus the web UI, and is the usual way to bring up a local stack.
The steps above are the minimal path for running the examples.

## Examples

### [quickstart.py](quickstart.py)

Basic usage of the four most common tools (search, explore, add, manage).

```bash
uv run python apps/api/examples/quickstart.py
```

### [task_workflow_example.py](task_workflow_example.py)

Full task management lifecycle including:

- Creating projects and tasks
- Task state transitions (start, block, unblock, review, complete)
- Automatic knowledge linking
- Learning capture and effort estimation

```bash
uv run python apps/api/examples/task_workflow_example.py
```

### [mcp_client_example.py](mcp_client_example.py)

Calling Sibyl as an MCP client over HTTP.

```bash
# First start the server
uv run sibyld serve

# Then run the client
uv run python apps/api/examples/mcp_client_example.py
```

## Tool Reference

The MCP server exposes eleven tools. The scripts above exercise the first four; the rest are
documented in [`apps/api/README.md`](../README.md).

| Tool               | Purpose                                                     |
| ------------------ | ----------------------------------------------------------- |
| `search`           | Unified semantic search across the graph and crawled docs   |
| `context`          | Compile an agent context pack (wake, recall, deep layers)   |
| `synthesis_plan`   | Plan a source-grounded synthesis from authorized memory     |
| `synthesis_draft`  | Draft, verify, and optionally remember a synthesis artifact |
| `synthesis_verify` | Verify citation, freshness, hidden-context, gap coverage    |
| `explore`          | Graph browse: list, related, traverse, dependencies         |
| `add`              | Create knowledge entities with auto-linking                 |
| `remember`         | Capture durable memory (decision, plan, idea, claim, ...)   |
| `reflect`          | Reflect raw notes into reviewable memory candidates         |
| `manage`           | State changes: task workflow, crawl, sync, analysis, admin  |
| `logs`             | Recent server logs (OWNER role)                             |

## Entity Types

Sibyl tracks roughly 29 entity types. The graph operates on a memory loop of recall, act, remember,
and reflect. A representative slice:

| Type       | Description                              |
| ---------- | ---------------------------------------- |
| `episode`  | Temporal knowledge (learnings, insights) |
| `pattern`  | Coding patterns and best practices       |
| `rule`     | Sacred rules and invariants              |
| `template` | Code templates and boilerplates          |
| `task`     | Work items with workflow states          |
| `epic`     | Multi-task initiatives                   |
| `project`  | Container for related tasks              |
| `source`   | Knowledge source (URL, file)             |
| `document` | Crawled or ingested content              |
| `decision` | Recorded choices and their rationale     |
| `plan`     | Intended sequences of work               |
| `idea`     | Candidate directions, not yet committed  |
| `claim`    | Assertions to be verified                |
| `note`     | Free-form captured memory                |
| `session`  | A unit of agent or user activity         |

See [`packages/python/sibyl-core/README.md`](../../../packages/python/sibyl-core/README.md) for the
complete entity model.
