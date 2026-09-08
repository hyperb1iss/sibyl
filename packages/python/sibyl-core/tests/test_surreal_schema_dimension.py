"""Embedding-dimension drift detection and HNSW rebuild on schema bootstrap."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from sibyl_core.backends.surreal import schema as schema_module
from sibyl_core.backends.surreal.schema import (
    EMBEDDING_DIM,
    EMBEDDING_VECTOR_FIELDS,
    GRAPH_SCHEMA_CURRENT_VERSION,
    bootstrap_schema,
    rebuild_embedding_indexes_for_dimension,
)


class _FakeSchemaDriver:
    """Minimal SchemaDriver stub that tracks recorded version/dimension and statements."""

    def __init__(
        self,
        *,
        url: str = "ws://localhost:8000/rpc",
        version: int | None = GRAPH_SCHEMA_CURRENT_VERSION,
        embedding_dimension: int | None = None,
        group_id: str = "org-1",
    ) -> None:
        self._url = url
        self._group_id = group_id
        self.version = version
        self.embedding_dimension = embedding_dimension
        self.statements: list[str] = []

    @property
    def group_id(self) -> str:
        return self._group_id

    @asynccontextmanager
    async def schema_lease_executor(self):
        yield self.execute_query

    async def execute_query(self, query: str, **params: object) -> object:
        stripped = query.strip()
        owned = stripped.startswith("BEGIN TRANSACTION;") and "$sibyl_schema_owned" in stripped
        if owned:
            stripped = (
                stripped.split("}; ", 1)[1].rsplit("\n;\nCOMMIT TRANSACTION;", 1)[0].strip() + ";"
            )
        self.statements.append(stripped)
        if "$sibyl_schema_claimed" in stripped:
            return None
        if stripped.startswith("SELECT owner FROM schema_lease:graph") and "owner" in params:
            return [{"owner": params["owner"]}]
        if stripped.startswith("UPDATE schema_lease:graph") and "RETURN AFTER" in stripped:
            return [{"owner": params["sibyl_schema_owner"]}]
        if stripped.startswith("INFO FOR TABLE"):
            return {
                "indexes": {field.index: "existing definition" for field in EMBEDDING_VECTOR_FIELDS}
            }
        if stripped.startswith("SELECT version FROM schema_version"):
            return [] if self.version is None else [{"version": self.version}]
        if stripped.startswith("SELECT embedding_dimension FROM schema_version"):
            if self.embedding_dimension is None:
                return [{"embedding_dimension": None}]
            return [{"embedding_dimension": self.embedding_dimension}]
        if stripped.startswith("UPSERT schema_version:graph"):
            self.version = int(params["version"])  # type: ignore[arg-type]
            recorded = params.get("embedding_dimension")
            if recorded is not None:
                self.embedding_dimension = int(recorded)  # type: ignore[arg-type]
            return None if owned else [{"version": self.version}]
        if stripped.startswith("INFO FOR INDEX"):
            return [{"building": {"status": "ready"}}]
        return None if owned else []


def _rebuild_statements(driver: _FakeSchemaDriver) -> list[str]:
    index_names = {vf.index for vf in EMBEDDING_VECTOR_FIELDS}
    return [
        s
        for s in driver.statements
        if s.startswith(("REMOVE INDEX", "DEFINE INDEX"))
        and any(index in s for index in index_names)
    ]


@pytest.mark.asyncio
async def test_bootstrap_rebuilds_indexes_on_dimension_change() -> None:
    stale_dimension = EMBEDDING_DIM + 256
    driver = _FakeSchemaDriver(embedding_dimension=stale_dimension)

    await bootstrap_schema(driver)  # type: ignore[arg-type]

    for vector_field in EMBEDDING_VECTOR_FIELDS:
        remove = f"REMOVE INDEX {vector_field.index} ON {vector_field.table};"
        define = (
            f"DEFINE INDEX {vector_field.index} ON {vector_field.table} "
            f"FIELDS {vector_field.field} HNSW DIMENSION {EMBEDDING_DIM}"
        )
        assert any(
            s.startswith(remove) and define in s and s.endswith("CONCURRENTLY;")
            for s in driver.statements
        )
        field_redef = vector_field.field_redefinition(EMBEDDING_DIM)
        assert field_redef in driver.statements
        assert vector_field.clear_statement() in driver.statements

    assert driver.embedding_dimension == EMBEDDING_DIM


@pytest.mark.asyncio
async def test_bootstrap_no_rebuild_when_dimension_matches() -> None:
    driver = _FakeSchemaDriver(embedding_dimension=EMBEDDING_DIM)

    await bootstrap_schema(driver)  # type: ignore[arg-type]

    assert _rebuild_statements(driver) == []
    assert driver.embedding_dimension == EMBEDDING_DIM


@pytest.mark.asyncio
async def test_bootstrap_stamps_dimension_when_unrecorded() -> None:
    driver = _FakeSchemaDriver(embedding_dimension=None)

    await bootstrap_schema(driver)  # type: ignore[arg-type]

    assert _rebuild_statements(driver) == []
    assert driver.embedding_dimension == EMBEDDING_DIM


@pytest.mark.asyncio
async def test_embedded_store_skips_rebuild_on_dimension_change() -> None:
    stale_dimension = EMBEDDING_DIM + 256
    driver = _FakeSchemaDriver(url="memory://", embedding_dimension=stale_dimension)

    await bootstrap_schema(driver)  # type: ignore[arg-type]

    assert _rebuild_statements(driver) == []
    assert driver.embedding_dimension == stale_dimension


@pytest.mark.asyncio
async def test_rebuild_covers_every_vector_field_and_records_dimension() -> None:
    target_dimension = EMBEDDING_DIM + 128
    driver = _FakeSchemaDriver(embedding_dimension=EMBEDDING_DIM)

    await rebuild_embedding_indexes_for_dimension(driver, dimension=target_dimension)  # type: ignore[arg-type]

    for vector_field in EMBEDDING_VECTOR_FIELDS:
        define = (
            f"DEFINE INDEX {vector_field.index} ON {vector_field.table} "
            f"FIELDS {vector_field.field} HNSW DIMENSION {target_dimension}"
        )
        assert any(define in s for s in driver.statements)
    assert driver.embedding_dimension == target_dimension


def test_embedding_vector_fields_cover_all_schema_hnsw_indexes() -> None:
    combined_schema = schema_module.NODE_DEFINITIONS + schema_module.EDGE_DEFINITIONS
    for vector_field in EMBEDDING_VECTOR_FIELDS:
        assert f"ON {vector_field.table} FIELDS {vector_field.field}\n    HNSW" in combined_schema
    # Every HNSW index baked into the schema must be represented in EMBEDDING_VECTOR_FIELDS,
    # so a dimension rebuild can never miss one.
    assert combined_schema.count("HNSW DIMENSION") == len(EMBEDDING_VECTOR_FIELDS)
