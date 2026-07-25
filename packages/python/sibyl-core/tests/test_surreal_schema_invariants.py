"""Checks that a schema plane is validated against reality, not its recorded version."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from surrealdb import AsyncSurreal

from sibyl_core.backends.surreal.auth_schema import auth_schema_invariant_plan
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_USAGE_SIGNAL_MIGRATION_DEFINITIONS,
    content_schema_invariant_plan,
)
from sibyl_core.backends.surreal.schema_helpers import split_statements
from sibyl_core.backends.surreal.schema_invariants import (
    SchemaInvariantPlan,
    check_schema_invariants,
    ensure_schema_invariants,
    expected_unique_indexes,
)
from sibyl_core.backends.surreal.schema_version import SchemaMigration, SurrealExecute

_USAGE_EVENT_TEMPLATE = """
CREATE memory_usage_events CONTENT {{
    uuid: '{uuid}',
    organization_id: 'org-a',
    session_key: 's',
    message_key: 'm',
    source_surface: 'recall',
    item_kind: 'raw_capture',
    item_id: '{item}',
    signal_type: 'exposure',
    metadata: {{}},
    event_at: d'2026-07-11T00:00:00Z',
    created_at: d'2026-07-11T00:00:00Z'
}};
"""


@pytest.fixture
async def db() -> AsyncIterator[AsyncSurreal]:
    connection = AsyncSurreal("memory://")
    await connection.use("invariants", "content")
    try:
        yield connection
    finally:
        await connection.close()


def _execute(connection: AsyncSurreal) -> SurrealExecute:
    async def run(statement: str, /, **params: object) -> object:
        return await connection.query(statement, params or None)

    return run


def test_expected_unique_indexes_ignores_non_unique_definitions() -> None:
    migrations = (
        SchemaMigration(
            version=1,
            name="one",
            statements=(
                "DEFINE INDEX IF NOT EXISTS idx_a ON widgets FIELDS uuid UNIQUE;",
                "DEFINE INDEX IF NOT EXISTS idx_b ON widgets FIELDS org, created_at;",
            ),
        ),
    )

    requirements = expected_unique_indexes(migrations)

    assert [item.name for item in requirements] == ["idx_a"]
    assert requirements[0].fields == ("uuid",)
    assert requirements[0].table == "widgets"


def test_expected_unique_indexes_retires_indexes_a_later_migration_drops() -> None:
    migrations = (
        SchemaMigration(
            version=1,
            name="one",
            statements=("DEFINE INDEX IF NOT EXISTS idx_url ON sources FIELDS url UNIQUE;",),
        ),
        SchemaMigration(
            version=2,
            name="two",
            statements=(
                "REMOVE INDEX IF EXISTS idx_url ON TABLE sources;",
                "DEFINE INDEX IF NOT EXISTS idx_org_url ON sources FIELDS org, url UNIQUE;",
            ),
        ),
    )

    assert [item.name for item in expected_unique_indexes(migrations)] == ["idx_org_url"]


def test_expected_unique_indexes_retires_an_index_redefined_without_unique() -> None:
    migrations = (
        SchemaMigration(
            version=1,
            name="one",
            statements=("DEFINE INDEX IF NOT EXISTS idx_a ON widgets FIELDS uuid UNIQUE;",),
        ),
        SchemaMigration(
            version=2,
            name="two",
            statements=("DEFINE INDEX OVERWRITE idx_a ON widgets FIELDS uuid;",),
        ),
    )

    assert expected_unique_indexes(migrations) == ()


def test_content_plan_requires_both_usage_event_dedupe_indexes() -> None:
    plan = content_schema_invariant_plan()
    names = {item.name for item in plan.unique_indexes}

    assert "idx_memory_usage_events_uuid" in names
    assert "idx_memory_usage_events_dedupe" in names
    assert "derived_from" in plan.relation_tables
    assert "memory_usage_events" in plan.schemafull_tables
    # v2 drops this index in favour of the org-scoped one; it must not be demanded back.
    assert "idx_crawl_sources_url" not in names


def test_auth_plan_requires_the_user_identity_indexes() -> None:
    names = {item.name for item in auth_schema_invariant_plan().unique_indexes}

    assert "idx_users_uuid" in names
    assert "idx_users_email" in names


@pytest.mark.asyncio
async def test_check_flags_a_schemaless_table_and_an_untyped_edge(db: AsyncSurreal) -> None:
    await db.query("CREATE widgets CONTENT { uuid: 'w1' };")
    await db.query("CREATE node_a:1; CREATE node_b:1;")
    await db.query("RELATE node_a:1->links->node_b:1 SET uuid = 'l1';")

    violations = await check_schema_invariants(
        _execute(db),
        SchemaInvariantPlan(schemafull_tables=("widgets",), relation_tables=("links",)),
    )

    assert {item.kind for item in violations} == {"table_mode", "table_type"}


@pytest.mark.asyncio
async def test_check_ignores_tables_that_do_not_exist_yet(db: AsyncSurreal) -> None:
    violations = await check_schema_invariants(
        _execute(db),
        SchemaInvariantPlan(schemafull_tables=("never_created",)),
    )

    assert violations == ()


@pytest.mark.asyncio
async def test_ensure_rebuilds_a_unique_index_that_was_skipped(db: AsyncSurreal) -> None:
    await db.query("DEFINE TABLE widgets SCHEMAFULL;")
    await db.query("DEFINE FIELD uuid ON widgets TYPE string;")
    await db.query("CREATE widgets CONTENT { uuid: 'w1' };")
    plan = SchemaInvariantPlan(
        schemafull_tables=("widgets",),
        unique_indexes=expected_unique_indexes(
            (
                SchemaMigration(
                    version=1,
                    name="one",
                    statements=(
                        "DEFINE INDEX IF NOT EXISTS idx_widgets_uuid "
                        "ON widgets FIELDS uuid UNIQUE;",
                    ),
                ),
            )
        ),
    )

    report = await ensure_schema_invariants(_execute(db), plan)

    assert report.ok
    assert report.repaired_indexes == ("idx_widgets_uuid",)
    assert report.unrepairable_indexes == ()


@pytest.mark.asyncio
async def test_ensure_reports_a_unique_index_duplicate_rows_still_block(db: AsyncSurreal) -> None:
    await db.query("DEFINE TABLE widgets SCHEMAFULL;")
    await db.query("DEFINE FIELD uuid ON widgets TYPE string;")
    await db.query("CREATE widgets CONTENT { uuid: 'w1' };")
    await db.query("CREATE widgets CONTENT { uuid: 'w1' };")
    plan = SchemaInvariantPlan(
        schemafull_tables=("widgets",),
        unique_indexes=expected_unique_indexes(
            (
                SchemaMigration(
                    version=1,
                    name="one",
                    statements=(
                        "DEFINE INDEX IF NOT EXISTS idx_widgets_uuid "
                        "ON widgets FIELDS uuid UNIQUE;",
                    ),
                ),
            )
        ),
    )

    report = await ensure_schema_invariants(_execute(db), plan)

    assert not report.ok
    assert [item.kind for item in report.violations] == ["unique_index"]
    assert report.repaired_indexes == ()
    assert [name for name, _ in report.unrepairable_indexes] == ["idx_widgets_uuid"]


@pytest.mark.asyncio
async def test_duplicate_rows_leave_dedupe_unenforced_after_a_clean_migration(
    db: AsyncSurreal,
) -> None:
    """The dishonest state the invariant check exists to catch.

    Duplicate rows make `DEFINE INDEX ... UNIQUE` fail. The bootstrap swallows that and
    records the migration as applied, so the plane sits at its target version while the
    deduplication the usage service depends on is not enforced at all.
    """
    for index in range(2):
        await db.query(_USAGE_EVENT_TEMPLATE.format(uuid="dup", item=f"item-{index}"))

    skipped: list[str] = []
    for statement in split_statements(CONTENT_USAGE_SIGNAL_MIGRATION_DEFINITIONS):
        if "memory_usage_events" not in statement:
            continue
        try:
            await db.query(statement)
        except Exception:
            skipped.append(statement)

    plan = content_schema_invariant_plan()
    usage_only = SchemaInvariantPlan(
        schemafull_tables=("memory_usage_events",),
        unique_indexes=tuple(
            item for item in plan.unique_indexes if item.table == "memory_usage_events"
        ),
    )
    report = await ensure_schema_invariants(_execute(db), usage_only)

    assert any("idx_memory_usage_events_uuid" in statement for statement in skipped)
    assert not report.ok
    assert any("idx_memory_usage_events_uuid" in item.detail for item in report.violations)

    await db.query("DELETE memory_usage_events WHERE item_id = 'item-1';")
    healed = await ensure_schema_invariants(_execute(db), usage_only)

    assert healed.ok
    assert "idx_memory_usage_events_uuid" in healed.repaired_indexes


@pytest.mark.parametrize(
    ("statement", "expected_fields"),
    [
        ("DEFINE INDEX idx_a ON widgets FIELDS uuid UNIQUE;", ("uuid",)),
        ("DEFINE INDEX idx_a ON widgets COLUMNS uuid UNIQUE;", ("uuid",)),
        ("DEFINE INDEX idx_a ON widgets FIELDS uuid UNIQUE CONCURRENTLY;", ("uuid",)),
        ("DEFINE INDEX idx_a ON widgets COLUMNS uuid UNIQUE CONCURRENTLY;", ("uuid",)),
        (
            "DEFINE INDEX IF NOT EXISTS idx_a ON TABLE widgets COLUMNS org, uuid UNIQUE;",
            ("org", "uuid"),
        ),
    ],
)
def test_expected_unique_indexes_reads_both_spellings_and_trailing_clauses(
    statement: str,
    expected_fields: tuple[str, ...],
) -> None:
    """COLUMNS is a synonym for FIELDS and UNIQUE is a modifier, not the final token.

    SurrealDB accepts every spelling here and normalizes them to the same index, so a
    parser that only reads `FIELDS ... UNIQUE;` silently drops a required invariant.
    """
    requirements = expected_unique_indexes(
        (SchemaMigration(version=1, name="one", statements=(statement,)),)
    )

    assert [item.name for item in requirements] == ["idx_a"]
    assert requirements[0].fields == expected_fields
    assert requirements[0].table == "widgets"


@pytest.mark.parametrize(
    "statement",
    [
        "DEFINE INDEX idx_a ON widgets FIELDS org, created_at;",
        "DEFINE INDEX idx_a ON widgets COLUMNS org, created_at CONCURRENTLY;",
        "DEFINE INDEX idx_a ON widgets FIELDS content FULLTEXT ANALYZER title_analyzer BM25;",
        (
            "DEFINE INDEX idx_a ON widgets FIELDS embedding "
            "HNSW DIMENSION 8 DIST COSINE TYPE F32 EFC 150 M 12;"
        ),
    ],
)
def test_expected_unique_indexes_ignores_indexes_that_promise_no_uniqueness(
    statement: str,
) -> None:
    assert (
        expected_unique_indexes((SchemaMigration(version=1, name="one", statements=(statement,)),))
        == ()
    )


@pytest.mark.asyncio
async def test_ensure_rebuilds_a_unique_index_declared_with_columns(db: AsyncSurreal) -> None:
    """The COLUMNS spelling has to survive the whole derive-then-rebuild round trip."""
    await db.query("DEFINE TABLE widgets SCHEMAFULL;")
    await db.query("DEFINE FIELD uuid ON widgets TYPE string;")
    await db.query("CREATE widgets CONTENT { uuid: 'w1' };")
    plan = SchemaInvariantPlan(
        schemafull_tables=("widgets",),
        unique_indexes=expected_unique_indexes(
            (
                SchemaMigration(
                    version=1,
                    name="one",
                    statements=(
                        "DEFINE INDEX IF NOT EXISTS idx_widgets_uuid "
                        "ON widgets COLUMNS uuid UNIQUE CONCURRENTLY;",
                    ),
                ),
            )
        ),
    )

    report = await ensure_schema_invariants(_execute(db), plan)

    assert report.ok
    assert report.repaired_indexes == ("idx_widgets_uuid",)
