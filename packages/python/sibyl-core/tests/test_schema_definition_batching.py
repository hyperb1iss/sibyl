"""Definition batches retain special statement and migration boundaries."""

from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal.schema_helpers import execute_schema_statements
from tests.test_schema_ownership import client as client


async def test_indexes_and_actions_delimit_fenced_definition_batches():
    calls = []

    async def execute(statement):
        calls.append(("single", statement))

    async def batch(statement):
        calls.append(("batch", statement))

    statements = (
        "DEFINE TABLE example SCHEMAFULL;",
        "DEFINE FIELD name ON example TYPE string;",
        "DEFINE INDEX name ON example FIELDS name UNIQUE;",
        "UPDATE example SET name='retained';",
        "DEFINE FIELD label ON example TYPE option<string>;",
    )
    await execute_schema_statements(execute, statements, scope="test", batch_execute=batch)
    assert calls == [
        ("batch", "\n".join(statements[:2])),
        ("single", statements[2]),
        ("single", statements[3]),
        ("batch", statements[4]),
    ]


async def test_failed_definition_batch_does_not_run_later_mutations():
    execute = AsyncMock()
    batch = AsyncMock(side_effect=RuntimeError("ownership lost"))
    with pytest.raises(RuntimeError, match="ownership lost"):
        await execute_schema_statements(
            execute,
            ("DEFINE TABLE example SCHEMAFULL;", "UPDATE example SET value=1;"),
            scope="test",
            batch_execute=batch,
        )
    execute.assert_not_awaited()


async def test_unowned_executor_keeps_individual_checked_statements():
    execute = AsyncMock()
    statements = ("DEFINE TABLE example SCHEMAFULL;", "DEFINE FIELD name ON example TYPE string;")
    await execute_schema_statements(execute, statements, scope="test")
    assert [call.args[0] for call in execute.await_args_list] == list(statements)


@pytest.mark.parametrize(
    "definition",
    [
        "DEFINE FIELD value ON rejected TYPE not_a_surreal_type;",
        "DEFINE TABLE retained SCHEMAFULL;",
    ],
)
async def test_malformed_later_definition_preserves_prior_migration_version(client, definition):
    from sibyl_core.backends.surreal.schema_ownership import try_acquire_schema_ownership
    from sibyl_core.backends.surreal.schema_version import (
        SchemaMigration,
        apply_schema_migrations,
        get_schema_version,
    )

    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    initial = SchemaMigration(1, "initial", ("DEFINE TABLE retained SCHEMALESS;",))
    try:
        await apply_schema_migrations(owner.read, [initial], ownership=owner)
        with pytest.raises(Exception, match=r"(?i)(parse|syntax|type|already)") as failure:
            await apply_schema_migrations(
                owner.read,
                [
                    initial,
                    SchemaMigration(
                        2,
                        "invalid",
                        (
                            "DEFINE TABLE rejected SCHEMAFULL;",
                            definition,
                        ),
                    ),
                ],
                ownership=owner,
            )
        if definition == "DEFINE TABLE retained SCHEMAFULL;":
            assert "already" in str(failure.value).lower()
            assert "parse" not in type(failure.value).__name__.lower()
        assert await get_schema_version(owner.read) == 1
        info = await client.execute_query("INFO FOR DB;")
        assert "retained" in info["tables"]
        assert "rejected" not in info["tables"]
    finally:
        await owner.release()


async def test_lease_loss_before_batch_transaction_cannot_define_schema(client):
    from sibyl_core.backends.surreal.schema_ownership import (
        SchemaOwnershipLost,
        try_acquire_schema_ownership,
    )

    owner = await try_acquire_schema_ownership(client.execute_query)
    assert owner is not None
    original = owner.execute

    async def expire_before_batch(statement, **params):
        if "DEFINE TABLE rejected" in statement:
            await original("UPDATE schema_lease:graph SET deadline=time::now()-1s;")
        return await original(statement, **params)

    owner.execute = expire_before_batch
    try:
        with pytest.raises(SchemaOwnershipLost):
            await execute_schema_statements(
                owner.mutate,
                (
                    "DEFINE TABLE rejected SCHEMAFULL;",
                    "DEFINE FIELD value ON rejected TYPE string;",
                ),
                scope="test",
                batch_execute=owner.mutate,
            )
        info = await original("INFO FOR DB;")
        assert "rejected" not in info["tables"]
    finally:
        await owner.release()
