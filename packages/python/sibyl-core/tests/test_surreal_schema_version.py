from __future__ import annotations

import pytest

from sibyl_core.backends.surreal.schema_version import (
    ConcurrentIndexDefinition,
    SchemaMigration,
    apply_schema_migrations,
    get_index_build_status,
    get_schema_version,
    rebuild_index_concurrently,
    record_schema_version,
    schema_version_record_id,
    wait_for_index_ready,
)


class _FakeSurreal:
    def __init__(self, *, version: int | None = None, index_status: str = "ready") -> None:
        self.version = version
        self.index_status = index_status
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, statement: str, **params: object) -> object:
        self.calls.append((statement, params))
        stripped = statement.strip()
        if stripped.startswith("SELECT version FROM schema_version"):
            return [] if self.version is None else [{"version": self.version}]
        if stripped.startswith("UPSERT schema_version:"):
            self.version = int(params["version"])
            return [{"version": self.version}]
        if stripped.startswith("INFO FOR INDEX"):
            return [{"building": {"status": self.index_status, "initial": 4, "pending": 0}}]
        return []


@pytest.mark.asyncio
async def test_schema_migrations_record_version_after_applying_new_steps() -> None:
    fake = _FakeSurreal()
    migrations = (
        SchemaMigration(version=1, name="first", statements=("DEFINE TABLE sample;",)),
        SchemaMigration(version=2, name="second", statements=("DEFINE INDEX idx ON sample;",)),
    )

    applied = await apply_schema_migrations(fake.execute_query, migrations, group_id="org-1")

    assert [migration.name for migration in applied] == ["first", "second"]
    assert fake.version == 2
    statements = [statement for statement, _ in fake.calls]
    assert "DEFINE TABLE sample;" in statements
    assert "DEFINE INDEX idx ON sample;" in statements


@pytest.mark.asyncio
async def test_schema_migrations_skip_applied_versions() -> None:
    fake = _FakeSurreal(version=1)
    migrations = (
        SchemaMigration(version=1, name="first", statements=("DEFINE TABLE sample;",)),
        SchemaMigration(version=2, name="second", statements=("DEFINE INDEX idx ON sample;",)),
    )

    applied = await apply_schema_migrations(fake.execute_query, migrations, group_id="org-1")

    assert [migration.name for migration in applied] == ["second"]
    statements = [statement for statement, _ in fake.calls]
    assert "DEFINE TABLE sample;" not in statements
    assert "DEFINE INDEX idx ON sample;" in statements


@pytest.mark.asyncio
async def test_schema_version_defaults_to_zero_when_missing() -> None:
    fake = _FakeSurreal()

    assert await get_schema_version(fake.execute_query) == 0


@pytest.mark.asyncio
async def test_schema_version_record_uses_stable_plane_id() -> None:
    fake = _FakeSurreal()

    await record_schema_version(
        fake.execute_query,
        name="content",
        version=1,
        migrations=(SchemaMigration(version=1, name="content_schema_bootstrap"),),
    )

    statement, params = fake.calls[-1]
    assert statement.strip().startswith("UPSERT schema_version:content SET")
    assert params["name"] == "content"
    assert params["migrations"] == [
        {"version": 1, "name": "content_schema_bootstrap"},
    ]


def test_schema_version_record_id_rejects_unsafe_names() -> None:
    with pytest.raises(ValueError, match="invalid SurrealDB identifier"):
        schema_version_record_id("content;DELETE")


@pytest.mark.asyncio
async def test_rebuild_index_concurrently_removes_and_defines_with_clause() -> None:
    fake = _FakeSurreal()
    definition = ConcurrentIndexDefinition(
        name="idx_entity_embedding",
        table="entity",
        definition="DEFINE INDEX idx_entity_embedding ON entity FIELDS name_embedding",
    )

    await rebuild_index_concurrently(fake.execute_query, definition)

    statements = [statement for statement, _ in fake.calls]
    assert statements == [
        "REMOVE INDEX IF EXISTS idx_entity_embedding ON TABLE entity;",
        "DEFINE INDEX idx_entity_embedding ON entity FIELDS name_embedding CONCURRENTLY;",
    ]


@pytest.mark.asyncio
async def test_index_status_parses_building_block() -> None:
    fake = _FakeSurreal(index_status="indexing")

    status = await get_index_build_status(fake.execute_query, name="idx", table="entity")

    assert status is not None
    assert status.status == "indexing"
    assert status.initial == 4
    assert status.pending == 0


@pytest.mark.asyncio
async def test_wait_for_index_ready_returns_ready_status() -> None:
    fake = _FakeSurreal(index_status="ready")

    status = await wait_for_index_ready(
        fake.execute_query,
        name="idx",
        table="entity",
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert status is not None
    assert status.status == "ready"


@pytest.mark.asyncio
async def test_concurrent_index_helpers_reject_unsafe_identifiers() -> None:
    fake = _FakeSurreal()
    definition = ConcurrentIndexDefinition(
        name="idx;DROP",
        table="entity",
        definition="DEFINE INDEX idx ON entity FIELDS name",
    )

    with pytest.raises(ValueError, match="invalid SurrealDB identifier"):
        await rebuild_index_concurrently(fake.execute_query, definition)


async def test_migration_action_failure_does_not_advance_version():
    fake = _FakeSurreal(version=21)
    attempts = []

    async def backfill(execute_query):
        attempts.append(True)
        assert fake.version == 21
        if len(attempts) == 1:
            raise RuntimeError("interrupted backfill")
        await execute_query("RETURN true;")

    migration = SchemaMigration(version=23, name="backfill", action=backfill)
    with pytest.raises(RuntimeError, match="interrupted backfill"):
        await apply_schema_migrations(fake.execute_query, [migration])
    assert fake.version == 21
    await apply_schema_migrations(fake.execute_query, [migration])
    assert fake.version == 23
    assert len(attempts) == 2


async def test_index_ready_requires_status_when_requested():
    async def execute_query(statement, **params):
        return [{}]

    with pytest.raises(RuntimeError, match="returned no build status"):
        await wait_for_index_ready(execute_query, name="idx", table="entity", require_status=True)
