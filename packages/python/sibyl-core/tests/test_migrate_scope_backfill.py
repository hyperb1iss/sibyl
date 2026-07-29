from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from sibyl_core.migrate.scope_backfill import (
    SCOPE_BACKFILL_SOURCE_KEY,
    SOURCE_DERIVED_ORG,
    SOURCE_DERIVED_PROJECT,
    SOURCE_RAW_CAPTURE,
    backfill_entity_scope_in_org,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import (
    EntityManager,
    SurrealGraphClient,
    normalize_records,
    prepare_graph_schema,
)


# Each test gets its own org: the embedded memory:// store is keyed by group
# id within the process, so a shared one lets rows from one test be scanned by
# the next and every count assertion becomes a lie.
def _org(name: str) -> str:
    return f"org-scope-{name}"


@asynccontextmanager
async def _graph(name: str) -> AsyncIterator[tuple[SurrealGraphClient, EntityManager]]:
    client = SurrealGraphClient(group_id=_org(name), url="memory://")
    try:
        await prepare_graph_schema(client)
        yield client, EntityManager(client, group_id=client.group_id)
    finally:
        await client.close()


def _lookup(table: dict[str, tuple[str, str | None, str | None]]) -> Any:
    async def raw_scope_lookup(raw_memory_id: str):
        return table.get(raw_memory_id)

    return raw_scope_lookup


async def _seed(manager: EntityManager, rows: list[Entity]) -> None:
    for row in rows:
        await manager.create_direct(row)


def _row(
    entity_id: str,
    *,
    entity_type: EntityType = EntityType.NOTE,
    metadata: dict[str, Any] | None = None,
) -> Entity:
    return Entity(
        id=entity_id,
        entity_type=entity_type,
        name=entity_id,
        content="body",
        metadata=metadata or {},
    )


async def _scopes(client: SurrealGraphClient) -> dict[str, dict[str, Any]]:
    rows = normalize_records(
        await client.execute_query(
            """
            SELECT uuid, memory_scope, attributes FROM entity
            WHERE group_id = $group_id ORDER BY uuid;
            """,
            group_id=client.group_id,
        )
    )
    return {
        str(row["uuid"]): {
            "column": row.get("memory_scope"),
            "attributes": row.get("attributes") or {},
        }
        for row in rows
    }


@pytest.mark.asyncio
async def test_a_graph_with_nothing_to_do_is_a_no_op() -> None:
    """A fresh install has no legacy rows, and the pass must not invent work."""
    async with _graph("nothing-to-do") as (client, manager):
        await _seed(manager, [_row("already", metadata={"memory_scope": "private"})])

        result = await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=False)

        assert result.success is True
        assert result.scanned == 0
        assert result.stamped == 0


@pytest.mark.asyncio
async def test_a_dry_run_counts_without_writing() -> None:
    async with _graph("dry-run") as (client, manager):
        await _seed(manager, [_row("scopeless")])

        result = await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=True)

        assert result.stamped == 1
        assert (await _scopes(client))["scopeless"]["column"] in (None, "")


@pytest.mark.asyncio
async def test_the_true_scope_is_recovered_before_any_default_applies() -> None:
    """Deriving first would stamp a genuinely private memory as org-readable.

    That converts a missing field into a real leak, so recovery has to win.
    """
    async with _graph("recovery-first") as (client, manager):
        # Carries a project_id too, so the derivation would have claimed it.
        await _seed(
            manager,
            [_row("was_private", metadata={"raw_memory_id": "raw-1", "project_id": "proj-a"})],
        )

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=_lookup({"raw-1": ("private", None, "user-1")}),
            dry_run=False,
        )

        assert result.recovered == 1
        assert result.derived_project == 0
        row = (await _scopes(client))["was_private"]
        assert row["column"] == "private"
        assert row["attributes"]["principal_id"] == "user-1"
        assert row["attributes"][SCOPE_BACKFILL_SOURCE_KEY] == SOURCE_RAW_CAPTURE


@pytest.mark.asyncio
async def test_a_project_row_keeps_its_project_gate_rather_than_widening() -> None:
    """Defaulting a project-gated row to org would widen who can read it."""
    async with _graph("project-gate") as (client, manager):
        await _seed(manager, [_row("in_project", metadata={"project_id": "proj-a"})])

        result = await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=False)

        assert result.derived_project == 1
        row = (await _scopes(client))["in_project"]
        assert row["column"] == "project"
        assert row["attributes"]["scope_key"] == "proj-a"
        assert row["attributes"][SCOPE_BACKFILL_SOURCE_KEY] == SOURCE_DERIVED_PROJECT


@pytest.mark.asyncio
async def test_a_row_with_no_project_becomes_org_readable_as_it_already_was() -> None:
    async with _graph("org-default") as (client, manager):
        await _seed(manager, [_row("loose")])

        result = await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=False)

        assert result.derived_org == 1
        row = (await _scopes(client))["loose"]
        assert row["column"] == "org"
        assert row["attributes"][SCOPE_BACKFILL_SOURCE_KEY] == SOURCE_DERIVED_ORG


@pytest.mark.asyncio
async def test_a_vanished_capture_falls_through_to_derivation() -> None:
    """A raw parent that no longer resolves is not an error, just no recovery."""
    async with _graph("vanished-capture") as (client, manager):
        await _seed(manager, [_row("orphaned", metadata={"raw_memory_id": "gone"})])

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=_lookup({}),
            dry_run=False,
        )

        assert result.recovered == 0
        assert result.derived_org == 1


@pytest.mark.asyncio
async def test_running_twice_changes_nothing_the_second_time() -> None:
    """The absent scope is the gate, so the pass is idempotent by construction."""
    async with _graph("idempotent") as (client, manager):
        await _seed(manager, [_row("a"), _row("b", metadata={"project_id": "proj-a"})])

        first = await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=False)
        after_first = await _scopes(client)
        second = await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=False)

        assert first.stamped == 2
        assert second.scanned == 0
        assert second.stamped == 0
        assert await _scopes(client) == after_first


@pytest.mark.asyncio
async def test_an_existing_stamp_is_never_downgraded() -> None:
    """The pass has no path to lowering a scope: it only selects absent ones."""
    async with _graph("no-downgrade") as (client, manager):
        await _seed(
            manager,
            [
                _row(
                    "stays_private",
                    metadata={"memory_scope": "private", "principal_id": "user-1"},
                )
            ],
        )

        await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=False)

        row = (await _scopes(client))["stays_private"]
        assert row["column"] == "private"
        assert SCOPE_BACKFILL_SOURCE_KEY not in row["attributes"]


@pytest.mark.asyncio
async def test_reverse_clears_only_what_the_pass_stamped() -> None:
    async with _graph("reverse-scoped") as (client, manager):
        await _seed(
            manager,
            [
                _row("stamped_by_us"),
                _row("stamped_before", metadata={"memory_scope": "private"}),
            ],
        )
        await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=False)

        result = await backfill_entity_scope_in_org(
            client, group_id=client.group_id, dry_run=False, reverse=True
        )

        scopes = await _scopes(client)
        assert result.scanned == 1
        assert not scopes["stamped_by_us"]["column"]
        assert SCOPE_BACKFILL_SOURCE_KEY not in scopes["stamped_by_us"]["attributes"]
        assert scopes["stamped_before"]["column"] == "private"


@pytest.mark.asyncio
async def test_a_mixed_graph_resolves_every_row_exactly_once() -> None:
    """Any user's graph is some mix; the counts must account for all of it."""
    async with _graph("mixed") as (client, manager):
        await _seed(
            manager,
            [
                _row("r1", metadata={"raw_memory_id": "raw-1"}),
                _row("r2", metadata={"raw_memory_id": "raw-2"}),
                _row("p1", metadata={"project_id": "proj-a"}),
                _row("p2", entity_type=EntityType.TASK, metadata={"project_id": "proj-b"}),
                _row("o1"),
                _row("already", metadata={"memory_scope": "project", "scope_key": "proj-z"}),
            ],
        )

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=_lookup(
                {
                    "raw-1": ("private", None, "user-1"),
                    "raw-2": ("team", "team-7", "user-2"),
                }
            ),
            dry_run=False,
        )

        assert result.scanned == 5
        assert (result.recovered, result.derived_project, result.derived_org) == (2, 2, 1)
        scopes = await _scopes(client)
        assert scopes["r1"]["column"] == "private"
        assert scopes["r2"]["column"] == "team"
        assert scopes["r2"]["attributes"]["scope_key"] == "team-7"
        assert scopes["p1"]["column"] == "project"
        assert scopes["p2"]["column"] == "project"
        assert scopes["o1"]["column"] == "org"
        assert scopes["already"]["column"] == "project"


@pytest.mark.asyncio
async def test_a_graph_larger_than_one_page_is_fully_covered() -> None:
    """Paging must not strand rows past the first page on a real user's graph.

    Both passes select on the field they then write, so a stamped page leaves
    the result set and the survivors slide down into offsets already consumed.
    Offset paging skipped exactly one page's worth of rows per page and still
    reported success, which would leave the fail-open unclosable: those rows
    would read as deliberately unscoped rather than as not-yet-migrated. The
    population has to exceed _PAGE_SIZE or every count here passes vacuously.
    """
    async with _graph("multi-page") as (client, manager):
        await manager.create_direct_bulk(
            [_row(f"bulk_{index:04d}") for index in range(620)],
            generate_embeddings=False,
        )

        result = await backfill_entity_scope_in_org(client, group_id=client.group_id, dry_run=False)

        assert result.scanned == 620
        assert result.derived_org == 620
        remaining = await backfill_entity_scope_in_org(
            client, group_id=client.group_id, dry_run=True
        )
        assert remaining.scanned == 0

        # Reverse pages the marker it wrote, so it strands rows the same way.
        undone = await backfill_entity_scope_in_org(
            client, group_id=client.group_id, dry_run=False, reverse=True
        )
        assert undone.scanned == 620
        assert all(not row["column"] for row in (await _scopes(client)).values())
