---
title: Knowledge Graph
description: Understanding Sibyl's graph architecture
---

# Knowledge Graph

Sibyl stores knowledge in a graph database, so entities can relate to each other directly and search
works by meaning. This guide explains how the graph works.

## Architecture Overview

Sibyl runs on a unified SurrealDB backend by default:

| Runtime | Storage                                                                     |
| ------- | --------------------------------------------------------------------------- |
| Default | Graph, content, auth, tasks, and memory in one SurrealDB-backed data plane. |

Existing FalkorDB installs should migrate through the archive playbook instead of starting new
legacy runtimes. See [storage-modes.md](./storage-modes.md).

### SurrealDB (default)

SurrealDB is a multi-model database. Sibyl uses it as the native graph, content, auth, task, and
memory store, with `org_<uuid_hex>` namespaces for per-org isolation. It provides:

- **SurrealQL queries**: graph traversal, full-text, and vector search in one language
- **HNSW vector indexes**: native embedding support for semantic recall
- **Embedded or remote**: SurrealKV for dev, WebSocket/HTTP for services

### Legacy FalkorDB Archives

FalkorDB was Sibyl's original Graphiti graph store. It now appears only as a migration source in old
archives or retained production installs that have not cut over yet. Active graph, content, RAG, and
auth runtime paths use SurrealDB.

### Graph Services

Sibyl's context retrieval loop runs through SurrealDB graph services. The graph managers and the
context search entry point live in the core services and retrieval packages:

```python
from sibyl_core.services.graph import EntityManager, RelationshipManager
from sibyl_core.retrieval.search import context_search
```

## Node Types

SurrealDB records use Sibyl entity and relationship types directly. Current runtime memories use
`entity` records; legacy `Episodic`/`Entity` archive shapes remain readable for migration
verification.

### Episode Entities

Temporal learnings, raw captures, and reflection output:

```bash
sibyl remember "Redis insight" "Connection pool must be >= concurrent requests" --kind rule
# Creates source-grounded memory records that native retrieval can render
```

### Entity Records

Structured graph records and extracted entities:

```python
# Tasks, projects, patterns, decisions, procedures, and artifacts
```

## Entity Types

Sibyl supports many entity types (see [Entity Types](./entity-types.md) for full details):

| Type       | Description                     |
| ---------- | ------------------------------- |
| `episode`  | Temporal learnings, discoveries |
| `pattern`  | Reusable coding patterns        |
| `rule`     | Sacred constraints, invariants  |
| `task`     | Work items with workflow        |
| `project`  | Container for tasks/epics       |
| `epic`     | Feature-level grouping          |
| `document` | Crawled content                 |
| `source`   | Documentation sources           |

## Relationships

Entities connect through typed relationships:

### Knowledge Relationships

| Type             | Usage                    |
| ---------------- | ------------------------ |
| `APPLIES_TO`     | Pattern applies to topic |
| `REQUIRES`       | A requires B             |
| `CONFLICTS_WITH` | Mutual exclusion         |
| `SUPERSEDES`     | A replaces B             |
| `RELATED_TO`     | Generic relationship     |
| `ENABLES`        | A enables B              |
| `BREAKS`         | A breaks B               |

### Task Relationships

| Type          | Usage                            |
| ------------- | -------------------------------- |
| `BELONGS_TO`  | Task -> Project, Epic -> Project |
| `DEPENDS_ON`  | Task -> Task (blocking)          |
| `BLOCKS`      | Task -> Task (inverse)           |
| `ASSIGNED_TO` | Task -> Person                   |
| `REFERENCES`  | Task -> Pattern/Rule             |

### Document Relationships

| Type           | Usage              |
| -------------- | ------------------ |
| `CRAWLED_FROM` | Document -> Source |
| `CHILD_OF`     | Document hierarchy |
| `MENTIONS`     | Document -> Entity |

Selected types shown. See [Entity Types](/guide/entity-types) for the complete list.

## Multi-Tenancy

Each organization gets its own isolated namespace:

```python
# Surreal: namespace named org_<uuid_hex>
# All operations require org context
manager = EntityManager(client, group_id=str(org.id))
```

::: danger Always Scope by Organization Never query without org scope. It routes to the wrong
namespace or breaks isolation. :::

## Write Concurrency

Each organization gets a connection-pooled SurrealDB client scoped to its namespace. The pool hands
out independent sockets (one query per socket at a time), so queries within an org run concurrently
with no single per-client query lock. `EntityManager` methods are safe to call concurrently; no
application-level locking is needed. Embedded and `memory://` URLs are clamped to a single
connection because a pool would fragment single-writer state.

## Hybrid Search

Search combines multiple techniques:

### Vector Search

Embeddings generated by OpenAI's embedding model enable semantic similarity:

```python
# Native context search fuses full-text, vector, raw memory, and graph expansion signals.
plan = build_context_retrieval_plan(...)
response = await context_search(plan=plan)
```

### BM25 Search

Keyword-based scoring for exact matches:

```python
# SurrealDB full-text indexes provide BM25-style exact-match scoring
# Combined with vector search via RRF fusion
```

### Reciprocal Rank Fusion (RRF)

Combines vector and keyword results:

```
RRF_score = sum(1 / (k + rank_i)) for each ranking
```

## Entity Storage

Entities store metadata as JSON in the `metadata` property:

```python
# Core properties stored directly
n.uuid          # Entity ID
n.name          # Display name
n.entity_type   # Type enum value
n.content       # Full content
n.description   # Summary

# Extended properties in metadata JSON
n.metadata = {
    "status": "doing",
    "priority": "high",
    "project_id": "proj_abc",
    "tags": ["backend", "auth"],
    ...
}
```

## Graph Creation Paths

### Direct Native Writes

```python
await manager.create_direct(entity)
```

- Creates native Surreal records immediately
- Preserves source and policy metadata
- Best for structured entities, task learnings, and reflection promotion

### Episode-Compatible Writes

```python
await manager.create(entity)
```

- Routes through the episode-shaped write path
- Used by flows that persist temporal episodes rather than structured entities

## Querying the Graph

### Using the Graph Runtime

```python
from sibyl_core.services.graph import get_surreal_graph_runtime

runtime = await get_surreal_graph_runtime(str(org_id))
manager = runtime.entity_manager

# Search
results = await manager.search("OAuth patterns", limit=10)

# Get by ID
entity = await manager.get("entity_abc")

# List by type
tasks = await manager.list_by_type(
    EntityType.TASK,
    status="todo",
    project_id="proj_123"
)
```

### Using RelationshipManager

```python
from sibyl_core.services.graph import RelationshipManager

rel_manager = RelationshipManager(client, group_id=str(org_id))

# Get related entities
related = await rel_manager.get_related_entities(
    entity_id="pattern_abc",
    relationship_types=[RelationshipType.APPLIES_TO],
    max_depth=2
)
```

### Direct SurrealQL Queries

For complex queries, use SurrealQL directly:

```python
result = await driver.execute_query(
    """
    SELECT id, name, status
    FROM entity
    WHERE entity_type = 'task'
      AND status = 'doing'
    """
)
```

Cypher (`MATCH`) applies only to legacy archive migration. Runtime queries against the active
SurrealDB store use SurrealQL.

## Best Practices

### 1. Always Use Org Context

```python
# WRONG
manager = EntityManager(client, group_id="")

# RIGHT
manager = EntityManager(client, group_id=str(org.id))
```

### 2. Query Native Entity Records

```surql
-- WRONG (scans every record before filtering)
SELECT * FROM entity;

-- RIGHT
SELECT * FROM entity WHERE entity_type = 'pattern';
```

### 3. Write Concurrency

`EntityManager` methods are safe to call concurrently. The Surreal driver handles serialization; no
application-level locking is needed.

### 4. Filter Early in Queries

```surql
-- WRONG (fetches all, filters in Python)
SELECT * FROM entity;

-- RIGHT (filters in DB)
SELECT * FROM entity
WHERE entity_type = $type
LIMIT 100;
```

## Troubleshooting

### Graph Corruption

Surreal mode: drop the per-org namespace from SurrealQL:

```surql
REMOVE NAMESPACE org_<uuid_hex>;
```

### Slow Queries

1. Add indexes for frequently queried properties
2. Limit result sets
3. Use specific node labels when possible

### Missing Results

1. Confirm the record exists in the org namespace's `entity` table
2. Verify org_id matches
3. Check if entity_type filter is correct

## Next Steps

- [Entity Types](./entity-types.md) - All available entity types
- [Semantic Search](./semantic-search.md) - Search in detail
- [Multi-Tenancy](./multi-tenancy.md) - Organization scoping
