"""Pytest fixtures for SurrealDB driver tests.

Each test gets an isolated in-memory SurrealDB instance with a fresh driver
scoped to a unique group_id. Tear-down closes the driver (and the embedded
engine with it) so no state leaks between tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio

from sibyl_core.backends.surreal import SurrealDriver


@pytest_asyncio.fixture
async def surreal_group_id() -> str:
    """Return a fresh org UUID per test."""
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def surreal_driver(surreal_group_id: str) -> AsyncIterator[SurrealDriver]:
    """Yield a connected, group-scoped SurrealDriver on in-memory storage."""
    base = SurrealDriver("memory://")
    driver = base.clone(surreal_group_id)
    try:
        yield driver
    finally:
        await driver.close()


@pytest_asyncio.fixture
async def surreal_schema(surreal_driver: SurrealDriver) -> SurrealDriver:
    """Yield a driver whose namespace has the full knowledge-graph schema."""
    await surreal_driver.build_indices_and_constraints()
    return surreal_driver
