from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    stamp_memory_scope_metadata,
)
from sibyl_core.migrate.scope_backfill import (
    SCOPE_BACKFILL_PRIOR_KEY,
    SCOPE_BACKFILL_SOURCE_KEY,
    SOURCE_DERIVED_PROJECT,
    SOURCE_RAW_CAPTURE,
    backfill_entity_scope_in_org,
    no_raw_scope_recovery,
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

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        assert result.success is True
        assert result.scanned == 0
        assert result.stamped == 0


@pytest.mark.asyncio
async def test_a_dry_run_counts_without_writing() -> None:
    async with _graph("dry-run") as (client, manager):
        await _seed(manager, [_row("scopeless", metadata={"project_id": "proj-a"})])

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=True,
        )

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

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        assert result.derived_project == 1
        row = (await _scopes(client))["in_project"]
        assert row["column"] == "project"
        assert row["attributes"]["scope_key"] == "proj-a"
        assert row["attributes"][SCOPE_BACKFILL_SOURCE_KEY] == SOURCE_DERIVED_PROJECT


@pytest.mark.asyncio
async def test_a_row_with_no_project_is_left_alone_rather_than_made_unreadable() -> None:
    """No scope value reproduces the fail-open's org readability.

    organization, shared, and public all reach scope_not_enabled, so stamping a
    projectless row with any of them revokes every reader it has. An earlier
    version wrote "org", which is not even a member of the enum: it coerced to
    None and denied everyone. Leaving the row unstamped preserves today's
    behavior and keeps it counted as outstanding work.
    """
    async with _graph("org-default") as (client, manager):
        await _seed(manager, [_row("loose")])
        reader = {"principal_id": "user-1", "private_scope_granted": False}
        before = memory_metadata_read_allowed({}, accessible_projects={"proj-a"}, **reader)

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        assert result.stamped == 0
        assert result.skipped_no_readable_scope == 1
        row = (await _scopes(client))["loose"]
        assert not row["column"]
        assert SCOPE_BACKFILL_SOURCE_KEY not in row["attributes"]
        after = memory_metadata_read_allowed(
            row["attributes"], accessible_projects={"proj-a"}, **reader
        )
        assert before is True
        assert after == before


@pytest.mark.asyncio
async def test_a_project_row_is_rekeyed_to_its_own_project_not_a_stale_audience() -> None:
    """A scope_key naming a different project is a widening if preserved.

    Before the stamp, authorization reads project_id; after, it reads
    scope_key. Keeping a stale key hands the row to that project's members
    instead of its own, so the derived key replaces it and the prior value is
    recorded for rollback.
    """
    async with _graph("stale-scope-key") as (client, manager):
        await _seed(manager, [_row("mixed", metadata={"project_id": "A", "scope_key": "B"})])
        reader = {"principal_id": "user-1", "private_scope_granted": False}
        before = memory_metadata_read_allowed(
            {"project_id": "A", "scope_key": "B"}, accessible_projects={"B"}, **reader
        )

        await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        attributes = (await _scopes(client))["mixed"]["attributes"]
        assert attributes["scope_key"] == "A"
        assert attributes[SCOPE_BACKFILL_PRIOR_KEY]["prior"]["scope_key"] == "B"
        after = memory_metadata_read_allowed(attributes, accessible_projects={"B"}, **reader)
        assert before is False
        assert after == before


@pytest.mark.asyncio
async def test_the_capture_owns_the_principal_rather_than_a_stale_row_value() -> None:
    """A stale principal_id would lock the owner out and admit a stranger.

    Private reads resolve the owner from principal_id, so deferring to whatever
    the legacy row carried gives that value private access and denies the
    capture's real owner.
    """
    async with _graph("stale-principal") as (client, manager):
        await _seed(
            manager,
            [_row("owned", metadata={"raw_memory_id": "raw-1", "principal_id": "stale-user"})],
        )

        await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=_lookup({"raw-1": ("private", None, "real-owner")}),
            dry_run=False,
        )

        attributes = (await _scopes(client))["owned"]["attributes"]
        assert attributes["principal_id"] == "real-owner"
        assert attributes[SCOPE_BACKFILL_PRIOR_KEY]["prior"]["principal_id"] == "stale-user"
        allowed = {"private_scope_granted": True, "accessible_projects": None}
        assert memory_metadata_read_allowed(attributes, principal_id="real-owner", **allowed)
        assert not memory_metadata_read_allowed(attributes, principal_id="stale-user", **allowed)


@pytest.mark.asyncio
async def test_a_scope_no_reader_can_satisfy_is_refused_not_stamped() -> None:
    """team and delegated are admitted by the policy but unreachable by reads.

    memory_metadata_read_allowed forwards neither membership set, so a row
    stamped with either is readable by nobody. Refusing keeps the row as it is
    instead of quietly deleting its audience.
    """
    async with _graph("unreadable-scope") as (client, manager):
        await _seed(manager, [_row("teamish", metadata={"raw_memory_id": "raw-1"})])

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=_lookup({"raw-1": ("team", "team-7", "user-2")}),
            dry_run=False,
        )

        assert result.stamped == 0
        assert result.skipped_unreadable == 1
        assert not (await _scopes(client))["teamish"]["column"]


@pytest.mark.asyncio
async def test_an_unknown_scope_from_a_capture_is_refused() -> None:
    """A value outside the enum coerces to None at the read check and denies."""
    async with _graph("unknown-scope") as (client, manager):
        await _seed(manager, [_row("weird", metadata={"raw_memory_id": "raw-1"})])

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=_lookup({"raw-1": ("nonsense", None, "user-1")}),
            dry_run=False,
        )

        assert result.stamped == 0
        assert result.skipped_unreadable == 1
        assert not (await _scopes(client))["weird"]["column"]


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
        assert result.skipped_no_readable_scope == 1


@pytest.mark.asyncio
async def test_running_twice_changes_nothing_the_second_time() -> None:
    """The absent scope is the gate, so the pass is idempotent by construction.

    A skipped row is still scoreless, so it is rescanned every run. That is the
    point: it stays visible as outstanding work rather than looking migrated.
    """
    async with _graph("idempotent") as (client, manager):
        await _seed(manager, [_row("a"), _row("b", metadata={"project_id": "proj-a"})])

        first = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )
        after_first = await _scopes(client)
        second = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        assert (first.stamped, first.skipped) == (1, 1)
        assert second.scanned == 1
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

        await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        row = (await _scopes(client))["stays_private"]
        assert row["column"] == "private"
        assert SCOPE_BACKFILL_SOURCE_KEY not in row["attributes"]


@pytest.mark.asyncio
async def test_reverse_clears_only_what_the_pass_stamped() -> None:
    async with _graph("reverse-scoped") as (client, manager):
        await _seed(
            manager,
            [
                _row("stamped_by_us", metadata={"project_id": "proj-a"}),
                _row("stamped_before", metadata={"memory_scope": "private"}),
            ],
        )
        await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
            reverse=True,
        )

        scopes = await _scopes(client)
        assert result.scanned == 1
        assert not scopes["stamped_by_us"]["column"]
        assert SCOPE_BACKFILL_SOURCE_KEY not in scopes["stamped_by_us"]["attributes"]
        assert scopes["stamped_before"]["column"] == "private"


@pytest.mark.asyncio
async def test_reverse_restores_what_the_pass_overwrote_rather_than_deleting_it() -> None:
    """Rollback has to be an inverse, not an approximation of one.

    An earlier version deleted scope_key from every row it had stamped, which
    destroyed a pre-existing key the forward pass had merely replaced. Reverse
    now reads the recorded prior value, so a round trip is the identity on every
    field the pass touches.
    """
    async with _graph("reverse-inverse") as (client, manager):
        seeded = {"project_id": "A", "scope_key": "B", "principal_id": "stale"}
        await _seed(manager, [_row("round_trip", metadata=dict(seeded))])
        before = (await _scopes(client))["round_trip"]["attributes"]

        await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )
        await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
            reverse=True,
        )

        after = (await _scopes(client))["round_trip"]["attributes"]
        for key, value in seeded.items():
            assert after.get(key) == value == before.get(key)
        assert SCOPE_BACKFILL_PRIOR_KEY not in after
        assert SCOPE_BACKFILL_SOURCE_KEY not in after


def test_the_write_path_refuses_to_carry_this_passes_provenance() -> None:
    """A caller may not supply this pass's provenance, and here is why.

    Reverse selects rows by its own marker and restores them from its own
    record, and a legitimate revert of a recovered row correctly returns it to
    scopeless -- that is the pre-migration state. So reverse cannot defend
    itself by inspection: a forged record is indistinguishable from a real one,
    and acting on it would strip a private row back to the fail-open.

    The boundary therefore has to be the write path, which is the only place
    caller metadata enters. Both markers join the owner fields it drops
    unconditionally. The forward pass also drops any it finds on a scopeless
    row, since it never writes one without a scope.
    """
    smuggled = stamp_memory_scope_metadata(
        {
            "note": "kept",
            SCOPE_BACKFILL_SOURCE_KEY: SOURCE_RAW_CAPTURE,
            SCOPE_BACKFILL_PRIOR_KEY: {"touched": ["memory_scope"], "prior": {}},
        },
        memory_scope="private",
        scope_key=None,
        principal_id="owner",
    )

    assert SCOPE_BACKFILL_SOURCE_KEY not in smuggled
    assert SCOPE_BACKFILL_PRIOR_KEY not in smuggled
    assert smuggled["note"] == "kept"
    assert smuggled["memory_scope"] == "private"


@pytest.mark.asyncio
async def test_the_forward_pass_drops_provenance_it_did_not_write() -> None:
    """A scopeless row carrying the markers did not get them from this pass.

    The forward pass writes them only alongside a scope and reverse removes
    both together, so finding one on a scopeless row means it arrived some
    other way. It is dropped rather than carried into the row's new record,
    where reverse would later restore from it.
    """
    async with _graph("stale-provenance") as (client, manager):
        await _seed(
            manager,
            [
                _row(
                    "smuggled",
                    metadata={
                        "project_id": "proj-a",
                        SCOPE_BACKFILL_SOURCE_KEY: SOURCE_RAW_CAPTURE,
                        SCOPE_BACKFILL_PRIOR_KEY: {
                            "touched": ["memory_scope"],
                            "prior": {"memory_scope": "public"},
                        },
                    },
                )
            ],
        )

        await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        attributes = (await _scopes(client))["smuggled"]["attributes"]
        assert attributes[SCOPE_BACKFILL_SOURCE_KEY] == SOURCE_DERIVED_PROJECT
        # The forged prior is gone: a revert restores scopeless, not "public".
        assert attributes[SCOPE_BACKFILL_PRIOR_KEY] == {
            "touched": ["memory_scope", "scope_key"],
            "prior": {},
        }


@pytest.mark.asyncio
async def test_a_partial_run_reports_the_batches_that_landed() -> None:
    """Writes commit per batch, so a failure is not the same as a no-op.

    Zeroing the counts on abort would describe a partially migrated org as
    untouched, which is the report that gets someone to re-run blind.
    """
    async with _graph("partial-run") as (client, manager):
        await manager.create_direct_bulk(
            [_row(f"p_{index:04d}", metadata={"project_id": "proj-a"}) for index in range(250)],
            generate_embeddings=False,
        )
        calls = {"n": 0}

        async def explodes_after_the_first_batch(raw_memory_id: str):
            calls["n"] += 1
            raise RuntimeError("capture store unavailable")

        await _seed(manager, [_row("needs_lookup", metadata={"raw_memory_id": "raw-1"})])
        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=explodes_after_the_first_batch,
            dry_run=False,
        )

        assert result.success is False
        assert result.errors
        assert calls["n"] == 1
        # The rows walked before the raise are reported, not silently dropped.
        assert result.scanned >= 1


@pytest.mark.asyncio
async def test_the_run_counts_what_it_could_not_reach() -> None:
    """A cursor cannot see a row that became scopeless behind it.

    So completeness is measured by a sweep after the walk. stampable_remaining
    is what says "run again", and it must not count rows the pass deliberately
    left alone.
    """
    async with _graph("remaining-sweep") as (client, manager):
        await _seed(
            manager,
            [_row("stampable", metadata={"project_id": "proj-a"}), _row("never_stampable")],
        )

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        assert result.stamped == 1
        # One row remains scopeless, and it is the one the pass refused.
        assert result.remaining == 1
        assert result.skipped == 1
        assert result.stampable_remaining == 0


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
                    # Admitted by the policy but unreachable by graph reads, so
                    # it is refused rather than stamped.
                    "raw-2": ("team", "team-7", "user-2"),
                }
            ),
            dry_run=False,
        )

        assert result.scanned == 5
        assert (result.recovered, result.derived_project) == (1, 2)
        assert (result.skipped_unreadable, result.skipped_no_readable_scope) == (1, 1)
        scopes = await _scopes(client)
        assert scopes["r1"]["column"] == "private"
        assert not scopes["r2"]["column"]
        assert scopes["p1"]["column"] == "project"
        assert scopes["p2"]["column"] == "project"
        assert not scopes["o1"]["column"]
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
        # Project rows, so the population is one the pass actually stamps: a
        # skipped row stays selectable and would mask a paging gap.
        await manager.create_direct_bulk(
            [_row(f"bulk_{index:04d}", metadata={"project_id": "proj-a"}) for index in range(620)],
            generate_embeddings=False,
        )

        result = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
        )

        assert result.scanned == 620
        assert result.derived_project == 620
        remaining = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=True,
        )
        assert remaining.scanned == 0

        # Reverse pages the marker it wrote, so it strands rows the same way.
        undone = await backfill_entity_scope_in_org(
            client,
            group_id=client.group_id,
            raw_scope_lookup=no_raw_scope_recovery,
            dry_run=False,
            reverse=True,
        )
        assert undone.scanned == 620
        assert all(not row["column"] for row in (await _scopes(client)).values())
