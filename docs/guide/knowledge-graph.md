---
title: Knowledge Graph
description: Understanding Sibyl's graph architecture
---

# Knowledge Graph

Sibyl stores knowledge in a graph database, enabling rich relationships between entities and
semantic search. This guide explains how the graph works.

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

- **SurrealQL queries** — graph traversal, full-text, and vector search in one language
- **HNSW vector indexes** — native embedding support for semantic recall
- **Embedded or remote** — RocksDB for dev, WebSocket/HTTP for services

### Legacy FalkorDB Archives

FalkorDB was Sibyl's original Graphiti graph store. It now appears only as a migration source in old
archives or retained production installs that have not cut over yet. Active graph, content, RAG, and
auth runtime paths use SurrealDB.

### Native Graph and Compatibility

Sibyl's default memory loop is moving through native SurrealDB services. Graphiti remains as a named
compatibility, compare-mode, admin, and migration surface while native retrieval and write coverage
finish their release gates:

```python
from sibyl_core.services.native_graph import NativeEntityManager, NativeRelationshipManager
from sibyl_core.retrieval.native import native_context_search
```

## Node Types

Native SurrealDB records use Sibyl entity and relationship types directly. Projectable legacy
Graphiti records still appear as `Episodic` and `Entity` nodes during the transition.

### Episodic Nodes

Temporal learnings, raw captures, and reflection output:

```python
# When you add knowledge via the CLI or MCP
sibyl add "Redis insight" "Connection pool must be >= concurrent requests"
# Creates source-grounded memory records that native retrieval can render
```

### Entity Nodes

Structured graph records, extracted entities, or projectable legacy nodes:

```python
# Tasks, projects, patterns, decisions, procedures, and artifacts
```

::: warning Query Native and Projectable Legacy Shapes Native retrieval keeps pre-v0.7 Graphiti
records queryable only when scope and source metadata can be projected safely:

```cypher
MATCH (n)
WHERE (n:Episodic OR n:Entity) AND n.entity_type = $type
RETURN n
```

:::

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
# Historical Falkor archives used a graph named by organization UUID
# All operations require org context
manager = EntityManager(client, group_id=str(org.id))
```

::: danger Always Scope by Organization Never query without org scope — it routes to the wrong
namespace or breaks isolation. :::

## Write Concurrency

The Surreal driver guards the WebSocket with a per-client `asyncio.Lock`. `EntityManager` methods
are safe to call concurrently; no application-level locking is needed.

## Hybrid Search

Search combines multiple techniques:

### Vector Search

Embeddings generated by OpenAI's embedding model enable semantic similarity:

```python
# Native context search fuses full-text, vector, raw memory, and graph expansion signals.
plan = build_native_context_retrieval_plan(...)
response = await native_context_search(plan=plan)
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

### Compatibility Extraction

```python
await manager.create(entity)
```

- Uses the retained Graphiti-shaped extraction path
- Available for unported flows while native extraction coverage matures
- Explicitly tracked in the Graphiti exit inventory

## Querying the Graph

### Using EntityManager

```python
from sibyl_core.graph import GraphClient, EntityManager

client = await GraphClient.create()
manager = EntityManager(client, group_id=str(org_id))

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
from sibyl_core.graph import RelationshipManager

rel_manager = RelationshipManager(client, group_id=str(org_id))

# Get related entities
related = await rel_manager.get_related_entities(
    entity_id="pattern_abc",
    relationship_types=[RelationshipType.APPLIES_TO],
    max_depth=2
)
```

### Direct Cypher Queries

For complex queries, use Cypher directly:

```python
result = await driver.execute_query(
    """
    MATCH (t:Entity)-[:BELONGS_TO]->(p:Entity)
    WHERE t.entity_type = 'task'
      AND p.uuid = $project_id
      AND t.status = 'doing'
    RETURN t.uuid, t.name, t.status
    """,
    project_id="proj_abc"
)
```

## Best Practices

### 1. Always Use Org Context

```python
# WRONG
manager = EntityManager(client, group_id="")

# RIGHT
manager = EntityManager(client, group_id=str(org.id))
```

### 2. Handle Both Node Labels

```cypher
-- WRONG (misses Episodic nodes)
MATCH (n:Entity) WHERE n.entity_type = 'pattern'

-- RIGHT
MATCH (n) WHERE (n:Episodic OR n:Entity) AND n.entity_type = 'pattern'
```

### 3. Write Concurrency

`EntityManager` methods are safe to call concurrently. The Surreal driver handles serialization; no
application-level locking is needed.

### 4. Filter Early in Queries

```cypher
-- WRONG (fetches all, filters in Python)
MATCH (n) RETURN n

-- RIGHT (filters in DB)
MATCH (n)
WHERE n.entity_type = $type AND n.group_id = $org_id
RETURN n
LIMIT 100
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

1. Check both `Episodic` and `Entity` labels
2. Verify org_id matches
3. Check if entity_type filter is correct

## Next Steps

- [Entity Types](./entity-types.md) - All available entity types
- [Semantic Search](./semantic-search.md) - Search in detail
- [Multi-Tenancy](./multi-tenancy.md) - Organization scoping
