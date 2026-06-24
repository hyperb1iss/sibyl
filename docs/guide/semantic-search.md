---
title: Semantic Search
description: How Sibyl's semantic search works
---

# Semantic Search

Sibyl's search goes beyond keyword matching to understand the _meaning_ of your queries. This guide
explains how semantic search works and how to use it effectively.

## How It Works

### Vector Embeddings

When you add knowledge to Sibyl, the content is converted into a vector embedding - a
high-dimensional numerical representation of meaning:

```
"OAuth refresh token implementation"
    -> [0.023, -0.041, 0.089, ..., 0.012]  (1536 dimensions)
```

Similar concepts produce similar vectors, enabling meaning-based search.

### Embedding Model

Sibyl uses OpenAI's embedding model (configurable):

```bash
# Default model
SIBYL_EMBEDDING_MODEL=text-embedding-3-small

# Higher quality (more expensive)
SIBYL_EMBEDDING_MODEL=text-embedding-3-large
```

### Hybrid Search

Search combines two techniques:

1. **Vector Search** - Cosine similarity between query and entity embeddings
2. **BM25 Search** - Traditional keyword scoring for exact matches

Results are merged using **Reciprocal Rank Fusion (RRF)**:

```
RRF_score = sum(1 / (k + rank)) for each ranking system
```

This ensures you get results that are either semantically similar OR keyword matches.

## Using Search

### Basic Search

```bash
# Search all entities
sibyl search "authentication patterns"

# The search finds related concepts even with different words
sibyl search "OAuth implementation"  # Finds "authentication patterns"
sibyl search "login security"         # Also matches
```

### Filtering by Type

```bash
# Search only patterns
sibyl search "error handling" --type pattern

# Search tasks
sibyl search "OAuth" --type task

# Multiple types
sibyl search "database" --type pattern,episode
```

### Scoping the Search Space

The `search` command keeps a small, focused flag set:

| Flag           | Purpose                                             |
| -------------- | --------------------------------------------------- |
| `--type`       | Filter by entity type (comma-separated)             |
| `--limit`      | Maximum results (default 10)                        |
| `--all`        | Search across all projects, not just the linked one |
| `--graph-only` | Search graph memory only                            |
| `--docs-only`  | Search crawled docs only                            |
| `--json`       | Structured output                                   |

```bash
# Search every project, not just the linked one
sibyl search "rate limiting" --all

# Graph memory only, no crawled docs
sibyl search "OAuth callback" --graph-only
```

`search` takes a required query. To list entities by structured filters such as status, project, or
assignee, use `sibyl task list` or `sibyl entity list` instead.

```bash
# Listing, not searching: structured filters live on task/entity list
sibyl task list --status todo --project proj_abc
sibyl entity list --type pattern
```

## Search vs Explore

Sibyl offers two ways to find entities:

| Feature             | `search`               | `explore`        |
| ------------------- | ---------------------- | ---------------- |
| **Purpose**         | Find by meaning        | Browse structure |
| **Input**           | Natural language query | Filters          |
| **Uses embeddings** | Yes                    | No               |
| **Good for**        | "Find related to X"    | "List all Y"     |

### When to Use Search

```bash
# Finding related knowledge
sibyl search "how to handle rate limiting"

# Discovering relevant patterns
sibyl search "retry logic best practices"

# Finding past solutions
sibyl search "Redis connection timeout"
```

### When to Use Entity List vs Explore

```bash
# List entities by type
sibyl entity list --type project
sibyl entity list --type task

# Explore graph relationships from a specific entity
sibyl explore related entity_xyz
sibyl explore traverse entity_xyz --depth 2
```

## Search in Code

### MCP Tool

```python
# Using the search MCP tool
result = await search(
    query="OAuth implementation patterns",
    types=["pattern", "episode"],
    language="python",
    limit=10
)

# Results include score
for item in result.results:
    print(f"{item.name}: {item.score:.3f}")
```

### EntityManager

```python
from sibyl_core.services.graph import EntityManager

manager = EntityManager(client, group_id=org_id)

# Semantic search
results = await manager.search(
    query="authentication patterns",
    entity_types=[EntityType.PATTERN, EntityType.EPISODE],
    limit=20
)

# Returns (entity, score) tuples
for entity, score in results:
    print(f"{entity.name}: {score:.3f}")
```

### Hybrid Search Module

For more control, use the hybrid search module directly:

Context packs use direct Surreal full-text, vector search, raw memory recall, graph neighborhood
expansion, and RRF fusion. `hybrid_search()` is the lower-level building block; the context loop
runs through `context_search()`.

```python
from sibyl_core.retrieval import hybrid_search, HybridConfig

config = HybridConfig(
    apply_temporal=True,       # Boost recent results
    temporal_decay_days=365,   # Decay constant
    graph_depth=2,             # Relationship traversal
)

result = await hybrid_search(
    query="OAuth patterns",
    client=client,
    entity_manager=manager,
    entity_types=[EntityType.PATTERN],
    limit=20,
    config=config,
    group_id=org_id,
)

# result.results contains (entity, score) tuples
```

## Temporal Boosting

By default, search boosts recent results:

```python
# Recent entities get higher scores
# Decay formula: score * exp(-days_old / decay_days)

from sibyl_core.retrieval import temporal_boost

boosted = temporal_boost(results, decay_days=365.0)
```

This helps surface fresh knowledge while keeping older relevant results.

## Search Tips

### 1. Use Natural Language

Search works best with natural language queries:

```bash
# GOOD - natural question
sibyl search "how to handle database connection timeouts"

# LESS GOOD - keyword style
sibyl search "database timeout handler"
```

### 2. Be Specific

More context helps find better matches:

```bash
# GOOD - specific context
sibyl search "Python asyncio task cancellation handling"

# LESS GOOD - too broad
sibyl search "async tasks"
```

### 3. Narrow with Type

Use the type filter to focus a search:

```bash
# Find patterns about authentication
sibyl search "authentication" --type pattern
```

### 4. Use the Right Tool for Listing

`search` always takes a query. To enumerate entities by structured filters, use `task list` or
`entity list`:

```bash
# List all patterns
sibyl entity list --type pattern

# List todo tasks in a project
sibyl task list --status todo --project proj_abc
```

## Document Search

Sibyl can also search crawled documentation. By default a search covers both graph memory and
crawled docs; narrow it with `--docs-only` or `--graph-only`:

```bash
# Docs only
sibyl search "Next.js middleware" --docs-only

# Graph memory only
sibyl search "OAuth callback" --graph-only
```

Document search uses the same hybrid approach over Surreal-backed document chunks. The MCP `search`
tool exposes finer filters such as `source_name` for narrowing to a single crawled source.

## Understanding Results

Search results include:

| Field           | Description                 |
| --------------- | --------------------------- |
| `id`            | Entity ID                   |
| `type`          | Entity type                 |
| `name`          | Entity name                 |
| `content`       | Content preview (truncated) |
| `score`         | Relevance score (0-1)       |
| `source`        | Source file or URL          |
| `result_origin` | "graph" or "document"       |

::: tip Get Full Content Search results show previews. Use `sibyl show <id>` to get full content.
:::

## Performance Considerations

### Result Limits

Always set reasonable limits:

```bash
sibyl search "query" --limit 20
```

### Index Efficiency

Vector search is fast, but filtering is applied post-search. For large graphs:

1. Use type filters to narrow the search space
2. Use project filters for task searches
3. Consider temporal filters for recent knowledge

### Caching

Embeddings are generated once when entities are created. Search queries generate a new embedding for
the query.

## Troubleshooting

### No Results

1. Check query isn't too specific
2. Verify entity type filter
3. Try broader search terms
4. Check organization context

### Irrelevant Results

1. Add more context to query
2. Use type filters
3. Try different wording

### Slow Search

1. Reduce limit
2. Add type filters
3. Check SurrealDB connection and query telemetry

## Next Steps

- [Capturing Knowledge](./capturing-knowledge.md) - Add searchable content
- [Entity Types](./entity-types.md) - Understand what to search for
- [Task Management](./task-management.md) - Search for tasks
