"""Content definition batches preserve checked migration boundaries."""

from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal import SurrealContentClient, content_schema
from sibyl_core.backends.surreal.schema_version import SchemaMigration, get_schema_version


@pytest.fixture
async def client():
    connection = SurrealContentClient(url="memory://")
    try:
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize(
    "definition",
    [
        "DEFINE FIELD value ON rejected TYPE not_a_surreal_type;",
        "DEFINE TABLE retained SCHEMAFULL;",
    ],
)
async def test_late_definition_failure_rolls_back_and_preserves_prior_version(
    client, monkeypatch, definition
):
    action = AsyncMock()
    migrations = (
        SchemaMigration(1, "initial", ("DEFINE TABLE retained SCHEMALESS;",)),
        SchemaMigration(
            2,
            "invalid",
            ("DEFINE TABLE rejected SCHEMAFULL;", definition),
            action=action,
        ),
        SchemaMigration(3, "later", ("DEFINE TABLE later SCHEMAFULL;",)),
    )
    monkeypatch.setattr(content_schema, "_content_schema_migrations", lambda **kwargs: migrations)

    with pytest.raises(Exception, match=r"(?i)(parse|syntax|type|already)") as failure:
        await content_schema.bootstrap_content_schema(client)

    if definition == "DEFINE TABLE retained SCHEMAFULL;":
        assert "already" in str(failure.value).lower()
        assert "parse" not in type(failure.value).__name__.lower()
    assert await get_schema_version(client.execute_query, name="content") == 1
    tables = (await client.execute_query("INFO FOR DB;"))["tables"]
    assert "retained" in tables
    assert "rejected" not in tables
    assert "later" not in tables
    action.assert_not_awaited()


async def test_content_batches_keep_index_event_data_action_and_version_order(client, monkeypatch):
    observed_versions = []

    async def backfill(execute_query):
        observed_versions.append(await get_schema_version(execute_query, name="content"))
        assert await execute_query("SELECT VALUE name FROM observed;") == ["first"]
        indexes = (await execute_query("INFO FOR TABLE sample;"))["indexes"]
        assert "idx_sample_name" in indexes
        await execute_query("UPDATE sample SET label = 'backfilled';")

    migrations = (
        SchemaMigration(
            1,
            "initial",
            (
                "DEFINE TABLE sample SCHEMAFULL;",
                "DEFINE FIELD name ON sample TYPE string;",
                "DEFINE TABLE observed SCHEMALESS;",
            ),
        ),
        SchemaMigration(
            2,
            "ordered",
            (
                "DEFINE FIELD label ON sample TYPE option<string>;",
                "DEFINE INDEX idx_sample_name ON sample FIELDS name UNIQUE;",
                "DEFINE EVENT record_creation ON sample WHEN $event = 'CREATE' "
                "THEN { CREATE observed SET name = $after.name; };",
                "CREATE sample:first SET name = 'first';",
                "DEFINE FIELD extra ON sample TYPE option<string>;",
            ),
            action=backfill,
        ),
    )
    monkeypatch.setattr(content_schema, "_content_schema_migrations", lambda **kwargs: migrations)
    execute = AsyncMock(wraps=client.execute_query)
    monkeypatch.setattr(client, "execute_query", execute)

    await content_schema.bootstrap_content_schema(client)

    assert observed_versions == [1]
    assert await get_schema_version(client.execute_query, name="content") == 2
    assert await client.execute_query("SELECT VALUE label FROM sample;") == ["backfilled"]
    statements = [call.args[0] for call in execute.await_args_list]
    batches = [statement for statement in statements if statement.startswith("BEGIN TRANSACTION;")]
    assert any(
        all(statement in batch for statement in migrations[0].statements) for batch in batches
    )
    for statement in migrations[1].statements[1:4]:
        assert statement in statements
        assert all(statement not in batch for batch in batches)


async def test_action_failure_keeps_completed_definitions_and_prior_version(client, monkeypatch):
    async def interrupted_action(execute_query):
        assert await get_schema_version(execute_query, name="content") == 1
        assert "next" in (await execute_query("INFO FOR DB;"))["tables"]
        raise RuntimeError("interrupted backfill")

    migrations = (
        SchemaMigration(1, "initial", ("DEFINE TABLE retained SCHEMALESS;",)),
        SchemaMigration(
            2,
            "backfill",
            ("DEFINE TABLE next SCHEMAFULL;", "DEFINE FIELD name ON next TYPE string;"),
            action=interrupted_action,
        ),
    )
    monkeypatch.setattr(content_schema, "_content_schema_migrations", lambda **kwargs: migrations)

    with pytest.raises(RuntimeError, match="interrupted backfill"):
        await content_schema.bootstrap_content_schema(client)

    assert await get_schema_version(client.execute_query, name="content") == 1
