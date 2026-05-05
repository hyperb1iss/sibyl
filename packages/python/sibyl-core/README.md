# sibyl-core

Core library for Sibyl. Domain models, graph operations, retrieval algorithms, and tool
implementations. Shared foundation for the API server and CLI.

## Quick Reference

```bash
# Install
uv add sibyl-core

# Development
moon run core:lint        # Ruff check
moon run core:typecheck   # Pyright
moon run core:test        # Pytest
```

## What's Here

- **models/:** Domain entities (Task, Project, Epic, Source, etc.)
- **graph/:** Graphiti client with pluggable backends (SurrealDB default, FalkorDB legacy)
- **backends/surreal/:** SurrealDB driver, schema, and per-table ops
- **retrieval/:** Graphiti node-hybrid search, fusion, deduplication
- **tools/:** MCP tool implementations (search, explore, add, manage)
- **tasks/:** Workflow engine, dependency resolution
- **auth/:** JWT primitives, password hashing

## Structure

```
src/sibyl_core/
├── models/
│   ├── entities.py       # Entity, EntityType, base classes
│   ├── tasks.py          # Task, Project, Epic, Milestone
│   ├── sources.py        # Source, Document
│   └── responses.py      # API response models
├── graph/
│   ├── client.py         # GraphClient (connection, write lock)
│   ├── entities.py       # EntityManager (CRUD, search)
│   └── relationships.py  # RelationshipManager
├── retrieval/
│   ├── hybrid.py         # Hybrid search orchestration
│   └── fusion.py         # Score fusion (RRF)
├── tools/
│   ├── search.py         # Semantic search
│   ├── explore.py        # Graph navigation
│   ├── add.py            # Entity creation
│   └── manage.py         # Task workflow, admin
└── tasks/
    ├── workflow.py       # State machine, transitions
    └── manager.py        # Task operations
```

## Usage

### Models

```python
from sibyl_core.models import (
    Entity, EntityType, Task, TaskStatus, Project, Epic,
)

task = Task(
    name="Implement OAuth",
    content="Add Google and GitHub OAuth",
    project_id="proj_abc",
    status=TaskStatus.TODO,
)
```

### Graph Client

```python
from sibyl_core.graph import GraphClient, EntityManager

client = GraphClient()
await client.connect()
manager = EntityManager(client, group_id=str(org_id))

# CRUD
await manager.create(entity)
# Retrieval uses search or list_by_type rather than direct ID lookup
results = await manager.search("authentication patterns", limit=20)
```

### Write Concurrency

Write concurrency is handled by the active graph driver. The SurrealDB driver serializes WebSocket
operations per client, and org-scoped graph access should use cloned drivers.

```python
# Direct writes go through the active graph backend
await client.execute_write_org(query, org_id, **params)

# Or use EntityManager
await manager.create(entity)
```

### Task Workflow

```python
from sibyl_core.tasks import TaskManager

manager = TaskManager(entity_manager, relationship_manager)
await manager.create_task_with_knowledge_links(task)
await manager.find_similar_tasks(task)
await manager.estimate_task_effort(task)
```

## Entity Types

| Type | Description |
|------|-------------|
| `pattern` | Reusable coding patterns |
| `episode` | Temporal learnings |
| `task` | Work items with workflow |
| `project` | Container for tasks |
| `epic` | Feature-level grouping |
| `source` | Documentation sources |
| `document` | Crawled content |

## Relationship Types

```python
from sibyl_core.models import RelationshipType

# Knowledge
RelationshipType.APPLIES_TO, REQUIRES, CONFLICTS_WITH, SUPERSEDES

# Task
RelationshipType.BELONGS_TO, DEPENDS_ON, BLOCKS, REFERENCES

```

## Configuration

```bash
SIBYL_OPENAI_API_KEY=sk-...         # Required (embeddings)
SIBYL_FALKORDB_HOST=localhost
SIBYL_FALKORDB_PORT=6380
SIBYL_ANTHROPIC_API_KEY=...         # Optional (LLM-powered features)
```

## Key Patterns

**Multi-tenancy:** Every operation requires org context
```python
manager = EntityManager(client, group_id=str(org.id))
```

**Node labels:** Graphiti creates `Episodic` and `Entity` nodes
```cypher
WHERE (n:Episodic OR n:Entity) AND n.entity_type = $type
```

**Creation paths:** LLM-powered (`create`) vs direct (`create_direct`)
```python
await manager.create(entity)         # Slower, richer extraction
await manager.create_direct(entity)  # Faster, no LLM
```

## Testing

```bash
# With mock LLM (fast, deterministic)
SIBYL_MOCK_LLM=true uv run pytest tests/

# Live model tests (costs money)
uv run pytest tests/live --live-models

# Retrieval benchmark suite
moon run core:bench-retrieval

# Live read-only search benchmark against a running stack
moon run core:bench-live

# Live context-pack smoke benchmark
moon run core:bench-context

# Save labeled artifacts for store-to-store comparison
moon run core:bench-live -- --label surreal --metadata store=surreal
```

`core:bench-live` probes the real `/api/search` path with CLI auth. `core:bench-context`
probes `/api/context/pack`; pass a JSON case file to turn smoke checks into dogfood
acceptance fixtures for coding handoffs, Haven recall, or other memory spaces. Both
benchmarks are read-only. Saved reports can be compared with
`uv run python benchmarks/compare_eval_reports.py <baseline.json> <candidate.json>`.
