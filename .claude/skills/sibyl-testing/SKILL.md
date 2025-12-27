---
name: sibyl-testing
description:
  Write tests for Sibyl using the test harness, mocks, and pytest patterns. Use when writing unit
  tests, integration tests, or testing MCP tools and graph operations.
---

# Sibyl Testing

## Test Harness Architecture

```
tests/
├── harness/
│   ├── mocks.py      # MockEntityManager, MockGraphClient
│   ├── context.py    # ToolTestContext (patches everything)
│   └── helpers.py    # call_search(), setup_entity_graph()
├── conftest.py       # Global fixtures
└── test_*.py         # Test modules
```

## Quick Start

```python
import pytest
from tests.harness import (
    ToolTestContext,
    MockEntityManager,
    create_test_entity,
)

@pytest.mark.asyncio
async def test_search_returns_results():
    ctx = ToolTestContext()
    entity = create_test_entity(entity_type="pattern", name="Auth Pattern")
    ctx.entity_manager.add_entity(entity)
    ctx.entity_manager.set_search_results([(entity, 0.95)])

    async with ctx.patch():
        from sibyl.tools.core import search
        result = await search("authentication")

        assert result.total >= 1
        assert "Auth" in result.results[0].name
```

## ToolTestContext

Patches all tool dependencies automatically:

```python
ctx = ToolTestContext()

# Pre-configure state
ctx.entity_manager.add_entity(entity)
ctx.entity_manager.set_search_results([...])
ctx.relationship_manager.add_relationship(rel)
ctx.graph_client.connected = True  # or False for error testing

async with ctx.patch():
    # All imports inside here use mocks
    from sibyl.tools.core import search, explore, add
    result = await search("query")
```

## Creating Test Entities

```python
from tests.harness import create_test_entity

# Basic entity
entity = create_test_entity()

# With specific attributes
entity = create_test_entity(
    entity_type="task",
    name="Fix auth bug",
    metadata={"status": "todo", "priority": "high"}
)

# Pattern entity
pattern = create_test_entity(
    entity_type="pattern",
    name="Repository Pattern",
    category="architecture"
)
```

## Testing Task Workflows

```python
@pytest.mark.asyncio
async def test_task_state_transition():
    ctx = ToolTestContext()
    task = create_test_entity(
        entity_type="task",
        metadata={"status": "todo"}
    )
    ctx.entity_manager.add_entity(task)

    async with ctx.patch():
        from sibyl.tools.manage import manage
        result = await manage(
            action="start_task",
            entity_id=task.id
        )

        assert result.success
        updated = ctx.entity_manager.get_entity(task.id)
        assert updated.metadata["status"] == "doing"
```

## Model Validation Tests

```python
import pytest
from pydantic import ValidationError
from sibyl.models.tasks import Task, TaskStatus

def test_task_requires_project_id():
    with pytest.raises(ValidationError):
        Task(
            id="t1",
            title="Test Task",
            # Missing project_id - should fail
        )

def test_task_title_max_length():
    with pytest.raises(ValidationError):
        Task(
            id="t1",
            title="x" * 201,  # Over 200 char limit
            project_id="p1"
        )
```

## Integration Tests

Mark with `@pytest.mark.integration` (requires live FalkorDB):

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_entity_crud():
    from sibyl.graph.client import get_graph_client
    from sibyl.graph.entities import EntityManager

    client = await get_graph_client()
    manager = EntityManager(client)

    # Test real operations
    entity_id = await manager.create_direct(test_entity)
    retrieved = await manager.get(entity_id)
    assert retrieved.name == test_entity.name
```

## Running Tests

```bash
just test                        # All tests
just test -k "test_search"       # Pattern match
just test -m integration         # Only integration
just test --cov=src/sibyl        # With coverage
```

## Pytest Configuration

```python
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "slow: marks tests as slow",
    "integration: marks tests as integration tests",
]
```
