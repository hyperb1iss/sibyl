"""Archive restore sends bounded batches and finishes after an interruption."""

import re
from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import source_archive_store
from sibyl_core.tools.admin import create_backup, restore_backup
from tests.test_graph_archive_atomicity import STAMP, episode, fingerprint
from tests.test_reflection_identity import content_store as content_store
from tests.test_source_integrity_archive import destination as destination
from tests.test_source_integrity_archive import runtime as runtime

BATCH = 2


def rows_carried(params: dict) -> dict[str, int]:
    """How many rows each list parameter carries.

    A lookup split into clumps (`name_0`, `name_1`, ...) counts as one list.
    """
    carried: dict[str, int] = {}
    for name, value in params.items():
        if isinstance(value, list):
            base = re.sub(r"_\d+$", "", name)
            carried[base] = carried.get(base, 0) + len(value)
    return carried


async def seed(runtime) -> None:
    await runtime.entity_manager.create_direct_bulk(
        [
            Entity(
                id=f"entity-{index}",
                entity_type=EntityType.TOPIC,
                name=f"Entity {index}",
                embedding=[float(index + 1)] + [0.0] * (EMBEDDING_DIM - 1),
            )
            for index in range(7)
        ]
    )
    await runtime.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id=f"edge-{index}",
                source_id=f"entity-{index}",
                target_id=f"entity-{index + 1}",
                relationship_type=RelationshipType.RELATED_TO,
            )
            for index in range(5)
        ]
    )
    for index in range(3):
        await episode(runtime.client, f"episode-{index}")


async def backup_of(runtime):
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.message
    backup.backup_data.mentions = [
        {
            "uuid": f"mention-{index}",
            "source_id": f"episode-{index}",
            "target_id": f"entity-{index}",
            "created_at": STAMP,
        }
        for index in range(3)
    ]
    backup.backup_data.mention_count = 3
    return backup.backup_data


async def restored_as_archived(destination, source) -> None:
    """The destination matches the source graph plus the archive's mentions."""
    expected = await inventory(source)
    expected["mentions"] = [{"uuid": f"mention-{index}"} for index in range(3)]
    assert await inventory(destination) == expected


async def inventory(client) -> dict[str, list]:
    async def rows(query):
        return normalize_records(await client.execute_query(query))

    return {
        "entities": await rows("SELECT uuid, name, name_embedding FROM entity ORDER BY uuid;"),
        "states": await rows(
            "SELECT source_id, generation, incarnation, deleted FROM source_states "
            "ORDER BY source_id;"
        ),
        "edges": await rows("SELECT uuid, source_id, target_id FROM relates_to ORDER BY uuid;"),
        "episodes": await rows("SELECT uuid FROM episode ORDER BY uuid;"),
        "mentions": await rows("SELECT uuid FROM mentions ORDER BY uuid;"),
    }


def record_requests(monkeypatch, client, *, fail_on=None) -> list[tuple[str, dict]]:
    """Record every request; optionally fail one write transaction by number."""
    sent: list[tuple[str, dict]] = []
    execute = client.execute_query
    writes = 0

    async def recording(query, **params):
        nonlocal writes
        sent.append((query, params))
        if query.startswith("BEGIN TRANSACTION"):
            writes += 1
            if writes == fail_on:
                raise ConnectionError("socket closed mid-restore")
        return await execute(query, **params)

    monkeypatch.setattr(client, "execute_query", recording)
    return sent


async def test_graph_restore_bounds_every_request_and_round_trips(
    runtime, destination, monkeypatch
):
    monkeypatch.setattr(source_archive_store, "ARCHIVE_RESTORE_BATCH_SIZE", BATCH)
    await seed(runtime)
    archive = await backup_of(runtime)
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    sent = record_requests(monkeypatch, destination.client)

    result = await restore_backup(archive, organization_id=runtime.client.group_id)

    assert result.success, result.errors
    assert (result.entities_restored, result.relationships_restored) == (9, 5)
    assert (result.episodes_restored, result.mentions_restored) == (3, 3)
    carried = [rows_carried(params) for _, params in sent]
    # A relationship names two endpoints; every other list holds one entry per row.
    endpoints = [
        rows.pop("relationship_endpoints") for rows in carried if "archive_relationships" in rows
    ]
    assert endpoints
    assert max(endpoints) <= 2 * BATCH
    for name in (
        "units",
        "unit_identities",
        "writes",
        "companion_uuids",
        "archive_episodes",
        "episode_uuids",
        "archive_relationships",
        "relationship_uuids",
        "archive_mentions",
        "mention_uuids",
    ):
        assert any(name in rows for rows in carried), name
    assert max(size for rows in carried for size in rows.values()) <= BATCH
    # 9 entities, 3 episodes, 5 relationships and 3 mentions in batches of two.
    assert sum(query.startswith("BEGIN TRANSACTION") for query, _ in sent) == 5 + 2 + 3 + 2
    await restored_as_archived(destination.client, runtime.client)


async def test_mentions_without_a_uuid_collapse_to_one_row_instead_of_failing(
    runtime, destination, monkeypatch
):
    await seed(runtime)
    archive = await backup_of(runtime)
    archive.mentions = [
        {key: value for key, value in mention.items() if key != "uuid"}
        for mention in archive.mentions[:2]
    ]
    archive.mention_count = 2
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )

    result = await restore_backup(archive, organization_id=runtime.client.group_id)

    assert result.success, result.errors
    assert len(await destination.client.execute_query("SELECT id FROM mentions;")) == 1


@pytest.mark.parametrize("clean", [False, True])
async def test_interrupted_graph_restore_keeps_whole_units_and_resumes(
    runtime, destination, monkeypatch, clean
):
    monkeypatch.setattr(source_archive_store, "ARCHIVE_RESTORE_BATCH_SIZE", BATCH)
    await seed(runtime)
    archive = await backup_of(runtime)
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    execute = destination.client.execute_query
    record_requests(monkeypatch, destination.client, fail_on=3)

    policy = {"clean": clean, "skip_existing": not clean}
    interrupted = await restore_backup(archive, organization_id=runtime.client.group_id, **policy)

    assert not interrupted.success
    assert any("socket closed" in error for error in interrupted.errors)
    partial = await inventory(destination.client)
    # Two batches of two units landed, each row with its ledger state.
    assert [row["uuid"] for row in partial["entities"]] == [
        "entity-0",
        "entity-1",
        "entity-2",
        "entity-3",
    ]
    source_states = {row["source_id"]: row for row in (await inventory(runtime.client))["states"]}
    assert partial["states"] == [source_states[row["uuid"]] for row in partial["entities"]]
    assert partial["edges"] == partial["episodes"] == partial["mentions"] == []

    monkeypatch.setattr(destination.client, "execute_query", execute)
    resumed = await restore_backup(archive, organization_id=runtime.client.group_id, **policy)

    assert resumed.success, resumed.errors
    assert not resumed.integrity_conflicts
    assert (resumed.entities_restored, resumed.entities_skipped) == (5, 4)
    await restored_as_archived(destination.client, runtime.client)


@pytest.mark.usefixtures("content_store")
async def test_clean_raw_restore_deletes_absent_sources_a_page_at_a_time(runtime, monkeypatch):
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.services import content_client
    from sibyl_core.services.surreal_content import remember_raw_memory

    monkeypatch.setattr(source_archive_store, "ARCHIVE_RESTORE_BATCH_SIZE", BATCH)
    org = runtime.client.group_id

    async def remember(text):
        return await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=text,
            raw_content=text,
            embedding_provider=None,
        )

    kept = [await remember(f"Archived {index}") for index in range(3)]
    async with content_client.surreal_content_client() as client:
        payload = await source_archive_store.export_source_integrity(
            client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=[org]
        )
        added = [await remember(f"Added after archive {index}") for index in range(5)]
        sent = record_requests(monkeypatch, client)

        result = await source_archive_store.restore_source_integrity(
            client.execute_query,
            payload,
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            clean=True,
        )

        assert not result["conflicts"]
        rows = normalize_records(await client.execute_query("SELECT uuid FROM raw_captures;"))
        assert {row["uuid"] for row in rows} == {memory.id for memory in kept}
        states = normalize_records(
            await client.execute_query(
                "SELECT source_id, deleted FROM source_states WHERE source_id IN $ids;",
                ids=[memory.id for memory in added],
            )
        )
        assert sorted(row["deleted"] for row in states) == [True] * 5
        deletes = [len(params["records"]) for query, params in sent if "DELETE $record" in query]
        assert sum(deletes) == 5
        assert max(deletes) <= BATCH


async def test_merge_refuses_an_edge_to_an_entity_the_archive_retires(
    runtime, destination, monkeypatch
):
    for identity in ("keeper", "retired"):
        await runtime.entity_manager.create_direct(
            Entity(id=identity, entity_type=EntityType.TOPIC, name=identity)
        )
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=runtime))
    first = await create_backup(organization_id=runtime.client.group_id)
    assert first.success, first.message
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    seeded = await restore_backup(first.backup_data, organization_id=runtime.client.group_id)
    assert seeded.success, seeded.errors
    # The source then deletes the entity, so the next archive retires it, while
    # an edge in that archive still points at it.
    assert await runtime.entity_manager.delete("retired")
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=runtime))
    archive = (await create_backup(organization_id=runtime.client.group_id)).backup_data
    archive.relationships.append(
        Relationship(
            id="edge-to-retired",
            source_id="keeper",
            target_id="retired",
            relationship_type=RelationshipType.RELATED_TO,
        ).model_dump(mode="json")
    )
    archive.relationship_count = len(archive.relationships)
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    before = await fingerprint(destination.client)

    result = await restore_backup(archive, organization_id=runtime.client.group_id)

    assert not result.success
    assert any("relationship endpoint is missing" in error for error in result.errors)
    assert await fingerprint(destination.client) == before
