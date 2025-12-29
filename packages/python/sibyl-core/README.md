# sibyl-core

Core library for Sibyl — domain models, graph operations, retrieval algorithms, and tool
implementations. This is the shared foundation used by both the API server and CLI.

## Overview

sibyl-core provides:

- **models/** — Domain entities (Entity, Task, Project, Epic, Source, etc.)
- **graph/** — FalkorDB/Graphiti client, entity management, relationship handling
- **retrieval/** — Hybrid search (semantic + BM25), fusion, deduplication, temporal ranking
- **tools/** — MCP tool implementations (search, explore, add, manage)
- **tasks/** — Workflow engine, dependency resolution, effort estimation
- **auth/** — JWT primitives, password hashing, auth context
- **utils/** — Resilience patterns, retry logic

## Installation

```bash
# As a dependency in another package
uv add sibyl-core

# For development
cd packages/python/sibyl-core
uv sync
```

## Structure

```
src/sibyl_core/
├── __init__.py           # Package exports
├── config.py             # CoreConfig settings
├── errors.py             # Exception hierarchy
│
├── models/               # Domain entities
│   ├── entities.py       # Entity, EntityType, base classes
│   ├── tasks.py          # Task, Project, Epic, Milestone
│   ├── sources.py        # Source, Document
│   ├── responses.py      # API response models
│   └── tools.py          # Tool input/output models
│
├── graph/                # FalkorDB operations
│   ├── client.py         # GraphClient (connection, write lock)
│   ├── entities.py       # EntityManager (CRUD, search)
│   ├── relationships.py  # RelationshipManager
│   ├── communities.py    # Community detection
│   ├── summarize.py      # Graph summarization
│   └── mock_llm.py       # Mock LLM for testing
│
├── retrieval/            # Search algorithms
│   ├── hybrid.py         # Hybrid search orchestration
│   ├── bm25.py           # BM25 scoring
│   ├── fusion.py         # Score fusion (RRF)
│   ├── dedup.py          # Result deduplication
│   └── temporal.py       # Time-based ranking
│
├── tools/                # MCP tool implementations
│   ├── core.py           # search, explore, add
│   ├── manage.py         # Task workflow, crawl, admin
│   └── admin.py          # Stats, health, backup
│
├── tasks/                # Task workflow
│   ├── workflow.py       # State machine, transitions
│   ├── manager.py        # Task operations
│   ├── dependencies.py   # Dependency graph
│   └── estimation.py     # Effort estimation
│
├── auth/                 # Auth primitives
│   ├── jwt.py            # Token creation/validation
│   ├── passwords.py      # Hashing, verification
│   └── context.py        # Auth context injection
│
└── utils/
    └── resilience.py     # Retry decorator, backoff
```

## Usage

### Models

```python
from sibyl_core.models import (
    Entity, EntityType,
    Task, TaskStatus, TaskPriority, TaskComplexity,
    Project, Epic,
    Source, Document,
)

# Create a task
task = Task(
    name="Implement OAuth",
    content="Add Google and GitHub OAuth providers",
    entity_type=EntityType.TASK,
    project_id="proj_abc",
    status=TaskStatus.TODO,
    priority=TaskPriority.HIGH,
    complexity=TaskComplexity.MEDIUM,
)
```

### Graph Client

```python
from sibyl_core.graph import GraphClient, EntityManager

# Initialize client
client = await GraphClient.create()

# Entity operations (always with org scope)
manager = EntityManager(client, group_id=str(org_id))

# Create entity
await manager.create(entity)

# Search
results = await manager.search("authentication patterns", limit=20)

# Get by ID
entity = await manager.get_by_id("entity_abc")

# List with filters
tasks = await manager.list_entities(
    types=[EntityType.TASK],
    status=TaskStatus.DOING,
    limit=50
)
```

### Write Concurrency

All writes must use the write lock to prevent FalkorDB corruption:

```python
# The client handles this internally, but if you need direct access:
async with client.write_lock:
    await client.execute_write_org(org_id, query, **params)

# Prefer using EntityManager methods which handle locking:
await manager.create(entity)      # Uses write lock
await manager.update(entity_id, updates)  # Uses write lock
await manager.delete(entity_id)   # Uses write lock
```

### Retrieval

```python
from sibyl_core.retrieval import HybridSearch

search = HybridSearch(client, org_id)

# Semantic + BM25 fusion search
results = await search.search(
    query="OAuth implementation patterns",
    types=[EntityType.PATTERN, EntityType.EPISODE],
    limit=20
)

# Results are deduplicated and ranked
for entity, score in results:
    print(f"{entity.name}: {score:.3f}")
```

### Tool Implementations

```python
from sibyl_core.tools import search, explore, add, manage

# Search (used by MCP server)
result = await search(
    ctx,  # MCP context
    query="authentication",
    types=["pattern"],
    limit=20
)

# Explore
result = await explore(
    ctx,
    mode="list",
    types=["project"]
)

# Add
result = await add(
    ctx,
    name="New pattern",
    content="Description...",
    entity_type="pattern"
)

# Manage
result = await manage(
    ctx,
    action="start_task",
    entity_id="task_abc"
)
```

### Task Workflow

```python
from sibyl_core.tasks import TaskWorkflow, TaskManager

workflow = TaskWorkflow()

# Valid transitions
workflow.can_transition(TaskStatus.TODO, TaskStatus.DOING)  # True
workflow.can_transition(TaskStatus.DONE, TaskStatus.TODO)   # True (any state)

# Task manager
manager = TaskManager(entity_manager)

await manager.start_task(task_id)
await manager.complete_task(task_id, learnings="Key insight...")
await manager.block_task(task_id, reason="Waiting on API access")
```

### Auth Primitives

```python
from sibyl_core.auth import create_token, verify_token, hash_password, verify_password

# JWT
token = create_token(user_id="user_abc", org_id="org_xyz")
claims = verify_token(token)

# Passwords
hashed = hash_password("secret123")
is_valid = verify_password("secret123", hashed)
```

### Resilience

```python
from sibyl_core.utils import retry, GRAPH_RETRY

# Decorator with config
@retry(config=GRAPH_RETRY)  # 3 attempts, exponential backoff
async def graph_operation():
    await client.execute_query(...)

# Custom config
from sibyl_core.utils import RetryConfig

@retry(config=RetryConfig(max_attempts=5, base_delay=0.5))
async def custom_retry():
    ...
```

## Configuration

Configuration via environment variables with `SIBYL_` prefix:

```python
from sibyl_core import core_config

# Access settings
core_config.falkordb_host      # SIBYL_FALKORDB_HOST
core_config.falkordb_port      # SIBYL_FALKORDB_PORT
core_config.openai_api_key     # SIBYL_OPENAI_API_KEY
core_config.anthropic_api_key  # SIBYL_ANTHROPIC_API_KEY
core_config.embedding_model    # SIBYL_EMBEDDING_MODEL
```

### Environment Variables

```bash
# Required
SIBYL_OPENAI_API_KEY=sk-...         # For embeddings

# FalkorDB
SIBYL_FALKORDB_HOST=localhost
SIBYL_FALKORDB_PORT=6380
SIBYL_FALKORDB_PASSWORD=conventions

# Optional
SIBYL_ANTHROPIC_API_KEY=...         # For LLM operations
SIBYL_EMBEDDING_MODEL=text-embedding-3-small
SIBYL_LOG_LEVEL=INFO
```

## Entity Types

| Type        | Class    | Description                     |
| ----------- | -------- | ------------------------------- |
| `pattern`   | Entity   | Reusable coding patterns        |
| `episode`   | Entity   | Temporal learnings, discoveries |
| `rule`      | Entity   | Sacred constraints, invariants  |
| `template`  | Entity   | Code templates                  |
| `tool`      | Entity   | Tool/library knowledge          |
| `language`  | Entity   | Language-specific knowledge     |
| `topic`     | Entity   | General topics                  |
| `task`      | Task     | Work items with workflow        |
| `project`   | Project  | Container for tasks             |
| `epic`      | Epic     | Feature-level grouping          |
| `team`      | Entity   | Team information                |
| `milestone` | Entity   | Project milestones              |
| `source`    | Source   | Documentation sources           |
| `document`  | Document | Crawled content                 |

## Relationship Types

```python
from sibyl_core.models import RelationshipType

# Knowledge relationships
RelationshipType.APPLIES_TO      # Pattern applies to topic
RelationshipType.REQUIRES        # A requires B
RelationshipType.CONFLICTS_WITH  # Mutual exclusion
RelationshipType.SUPERSEDES      # A replaces B
RelationshipType.ENABLES         # A enables B
RelationshipType.BREAKS          # A breaks B

# Task relationships
RelationshipType.BELONGS_TO      # Task belongs to project
RelationshipType.DEPENDS_ON      # Task depends on task
RelationshipType.BLOCKS          # Task blocks task
RelationshipType.ASSIGNED_TO     # Task assigned to team
RelationshipType.REFERENCES      # References entity
RelationshipType.ENCOUNTERED     # Error encountered

# Doc relationships
RelationshipType.CRAWLED_FROM    # Crawled from source
RelationshipType.CHILD_OF        # Parent/child structure
RelationshipType.MENTIONS        # Mentions entity
```

## Development

### moonrepo Tasks

```bash
moon run core:lint        # Ruff check
moon run core:format      # Ruff format
moon run core:typecheck   # Pyright
moon run core:test        # Pytest
moon run core:check       # All of the above
```

### Direct Commands

```bash
cd packages/python/sibyl-core

uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pyright src/
uv run pytest tests/ -v
```

## Key Patterns

### Multi-Tenancy

Every graph operation requires organization context:

```python
# EntityManager always needs group_id
manager = EntityManager(client, group_id=str(org.id))

# Direct queries need org scope
await client.execute_query_org(org_id, query, **params)
```

### Node Labels

Graphiti creates two node types:

- `Episodic` — Created by `add_episode()`, has `entity_type` property
- `Entity` — Extracted entities, may lack `entity_type`

Queries must handle both:

```cypher
MATCH (n)
WHERE (n:Episodic OR n:Entity) AND n.entity_type = $type
RETURN n
```

### Entity Creation Paths

```python
# LLM-powered (slower, richer entity extraction)
await manager.create(entity)

# Direct insertion (faster, no LLM)
await manager.create_direct(entity)
```

## Testing

The package includes test utilities:

```python
from sibyl_core.graph import MockLLMClient

# Mock LLM for deterministic tests
mock_client = MockLLMClient()
mock_client.set_response("Expected response")
```

Run tests with mock LLM:

```bash
SIBYL_MOCK_LLM=true uv run pytest tests/
```

## Dependencies

Core dependencies:

- `graphiti-core[falkordb,anthropic]` — Graph RAG framework
- `pydantic` / `pydantic-settings` — Data validation
- `httpx` — HTTP client
- `structlog` — Logging
- `crawl4ai` — Web crawling
- `python-louvain` — Community detection
