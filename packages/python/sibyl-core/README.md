# sibyl-core

Core library for Sibyl. Domain models, graph operations, retrieval algorithms, the AI
substrate, and tool implementations. Shared foundation for the API server and CLI.

## Quick Reference

```bash
# Install
uv add sibyl-core

# Development
moon run core:lint        # Ruff check
moon run core:typecheck   # ty
moon run core:test        # Pytest
```

## What's Here

- **models/:** Domain entities (Task, Project, Epic, Source, reflection, synthesis)
- **graph/:** SurrealDB graph managers plus Graphiti compatibility adapters
- **backends/surreal/:** SurrealDB driver, schema, and per-table operations
- **retrieval/:** Native context-pack retrieval, compatibility search, fusion, dedup
- **ai/:** Native LLM substrate, model registry, providers, validation
- **embeddings/:** Embedding provider clients
- **services/:** Memory loop, reflection, synthesis, autonomy, source adapters
- **tools/:** MCP tool implementations
- **tasks/:** Workflow engine and dependency resolution
- **migrate/:** Migration archive merge and rewrite logic
- **auth/:** JWT primitives and password hashing

## Structure

```
src/sibyl_core/
├── models/
│   ├── entities.py       # Entity, EntityType, base classes
│   ├── tasks.py          # Task, Project, Epic, Milestone
│   ├── sources.py        # Source, Document
│   ├── context.py        # Context-pack models
│   ├── reflection.py     # Reflection candidate models
│   ├── synthesis.py      # Synthesis plan and artifact models
│   └── responses.py      # API response models
├── graph/
│   └── surreal/          # SurrealDB graph managers
├── backends/surreal/     # Driver, schema, table operations
├── retrieval/            # Native context retrieval, fusion (RRF), dedup
├── ai/
│   ├── registry.py       # Curated LLM/embedding model registry
│   ├── providers.py      # PydanticAI provider model factory
│   ├── clients.py        # Scoped agent caching
│   └── llm/              # Extractor, Generator, config sources
├── embeddings/           # Embedding provider clients
├── services/             # Memory loop, reflection, synthesis, source adapters
├── tools/                # MCP tool implementations
└── tasks/                # Workflow state machine, dependency resolution
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

The SurrealDB driver serializes WebSocket operations per client, and org-scoped graph
access should use cloned drivers.

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

### AI Substrate

```python
from pydantic import BaseModel

from sibyl_core.ai import Extractor, Generator, LLMSurface


class ExtractedFact(BaseModel):
    name: str
    summary: str


extractor = Extractor(ExtractedFact, surface=LLMSurface.CRAWLER)
fact = await extractor.extract("Extract one fact from this document chunk.")

generator = Generator(surface=LLMSurface.SYNTHESIS)
draft = await generator.generate("Summarize this context pack.", max_tokens=512)
```

The substrate uses PydanticAI under `sibyl_core.ai`, with provider API keys passed
through provider objects rather than mutating `os.environ`. `Extractor[T]` handles
structured output and classified LLM errors. `Generator` handles text generation and
streaming. Surface-specific config is resolved through an `LLMConfigSource` so the API
can supply database-backed settings while core stays pure.

## Entity Types

Sibyl models a broad set of entity types so memory stays structured. The registry
lives in `models/entities.py` and covers, among others:

- **Work:** `task`, `epic`, `project`, `milestone`
- **Knowledge:** `pattern`, `episode`, `procedure`, `rule`, `guide`, `error_pattern`
- **Memory:** `decision`, `plan`, `idea`, `claim`, `artifact`, `session`, `note`
- **Sources:** `source`, `document`, `domain`, `community`

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
SIBYL_LLM_PROVIDER=anthropic          # anthropic | openai
SIBYL_LLM_MODEL=claude-haiku-4-5
SIBYL_LLM_TEMPERATURE=0
SIBYL_LLM_MAX_TOKENS=2048
SIBYL_LLM_TIMEOUT_SECONDS=60

# Surface-specific values override shared LLM values.
SIBYL_LLM_CRAWLER_PROVIDER=gemini
SIBYL_LLM_CRAWLER_MODEL=gemini-3-1-flash-lite
SIBYL_LLM_SYNTHESIS_PROVIDER=anthropic
SIBYL_LLM_SYNTHESIS_MODEL=claude-sonnet-4-6

SIBYL_ANTHROPIC_API_KEY=...           # LLM provider key
SIBYL_OPENAI_API_KEY=sk-...           # LLM or embedding provider key
SIBYL_GEMINI_API_KEY=...              # LLM or embedding provider key

SIBYL_EMBEDDING_PROVIDER=openai       # openai | gemini
SIBYL_EMBEDDING_MODEL=text-embedding-3-small
SIBYL_EMBEDDING_DIMENSIONS=1536
SIBYL_GRAPH_EMBEDDING_PROVIDER=openai
SIBYL_GRAPH_EMBEDDING_MODEL=text-embedding-3-small
SIBYL_GRAPH_EMBEDDING_DIMENSIONS=1024
```

LLM settings are instance-wide. Environment variables win over database settings and
mark individual fields as locked.

Gemini keys can also come from `GEMINI_API_KEY` or `GOOGLE_API_KEY`. Changing embedding
provider, model, or dimensions requires re-embedding existing graph and document
vectors before comparing old and new search results.

To add a first-class LLM provider, add a provider factory branch in
`sibyl_core.ai.providers`, add registry entries in `sibyl_core.ai.registry`, extend
`LLMProviderName` and the API DTOs, and add a live probe to
`scripts/llm/verify_registry.py`.

## Key Patterns

**Multi-tenancy:** Every operation requires org context.

```python
manager = EntityManager(client, group_id=str(org.id))
```

**Node shapes:** Native retrieval queries direct Surreal records and projectable
legacy `Episodic`/`Entity` records.

```cypher
WHERE (n:Episodic OR n:Entity) AND n.entity_type = $type
```

**Creation paths:** direct native writes first, compatibility extraction when
explicitly needed.

```python
await manager.create_direct(entity)  # Native write path, no LLM
await manager.create(entity)         # Compatibility extraction path
```

## Legacy Compatibility

Legacy Graphiti-shaped records remain readable through Sibyl-owned Surreal
projection and archive code. The package no longer exposes a Graphiti
compatibility extra or installs `graphiti-core`.

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
```

`core:bench-live` probes the real `/api/search` path with CLI auth. `core:bench-context`
probes `/api/context/pack`. Both benchmarks are read-only. Saved reports can be compared
with `uv run python benchmarks/compare_eval_reports.py <baseline.json> <candidate.json>`.
