---
name: sibyl-backend
description:
  Python backend development for Sibyl including Graphiti integration, FalkorDB queries, async
  patterns, and the 4-tool MCP API. Use when implementing backend features, graph operations, entity
  managers, or MCP tools.
---

# Sibyl Backend Development

## Architecture Overview

```
src/sibyl/
├── server.py          # MCP tool registration (FastMCP)
├── main.py            # Combined app factory (Starlette)
├── graph/
│   ├── client.py      # GraphClient (Graphiti wrapper)
│   ├── entities.py    # EntityManager (CRUD, search)
│   └── relationships.py
├── tools/
│   ├── core.py        # search/explore/add implementations
│   └── manage.py      # Task workflow actions
└── models/            # Pydantic entities
```

## Entity Creation

Two paths exist:

```python
# Path 1: LLM extraction (slower, semantic)
await entity_manager.create(entity)  # Uses Graphiti add_episode

# Path 2: Direct insertion (faster, structured data)
await entity_manager.create_direct(entity)  # Direct Cypher MERGE
```

Use `create_direct()` for batch imports and structured data (tasks, projects). Use `create()` when
you want Graphiti's LLM to extract entities and relationships.

## Querying Nodes - Critical Pattern

Graphiti creates **two different node labels**:

- `Episodic` - Created by `add_episode()`, has our `entity_type` property
- `Entity` - Extracted by Graphiti's LLM, may NOT have `entity_type`

**Always query both labels:**

```python
result = await client.driver.execute_query(
    """
    MATCH (n)
    WHERE (n:Episodic OR n:Entity)
      AND n.entity_type = $type
      AND n.group_id = 'conventions'
    RETURN n
    """,
    type=entity_type.value,
)
```

**Common mistake:** Using `MATCH (n:Entity)` only finds LLM-extracted nodes, missing all our
`Episodic` nodes from `add_episode()`.

## Async Patterns

```python
# CLI commands use @run_async decorator
@app.command()
def my_command():
    @run_async
    async def _impl():
        client = await get_graph_client()
        manager = EntityManager(client)
        # ...
    _impl()

# Resilience with retry
from sibyl.utils.resilience import retry, GRAPH_RETRY

@retry(config=GRAPH_RETRY)
async def risky_operation():
    ...
```

## Adding New MCP Tools

1. Add implementation in `tools/core.py` or `tools/manage.py`
2. Register in `server.py`:

```python
@mcp.tool()
async def my_tool(param: str) -> dict:
    """Tool description for agent discovery."""
    return await core_implementation(param)
```

## Error Handling

```python
from sibyl.errors import (
    EntityNotFoundError,
    EntityCreationError,
    GraphConnectionError,
    InvalidTransitionError,
)

try:
    entity = await manager.get(entity_id)
except EntityNotFoundError:
    # Handle missing entity
```

## Environment Configuration

Key settings in `.env`:

```bash
SIBYL_FALKORDB_PORT=6380           # Note: 6380, not 6379
SIBYL_FALKORDB_PASSWORD=conventions
SIBYL_FALKORDB_GRAPH_NAME=conventions
SEMAPHORE_LIMIT=10                  # Graphiti's internal concurrency
```

## Testing Backend Code

```python
from tests.harness import ToolTestContext, create_test_entity

async def test_my_feature():
    ctx = ToolTestContext()
    entity = create_test_entity(entity_type="task")
    ctx.entity_manager.add_entity(entity)

    async with ctx.patch():
        result = await my_function()
        assert result.success
```

## FalkorDB Notes

- Port 6380 (not 6379) to avoid Redis conflicts
- Use BlockingConnectionPool to handle connection exhaustion gracefully
- `SEMAPHORE_LIMIT` controls Graphiti's internal parallelism
