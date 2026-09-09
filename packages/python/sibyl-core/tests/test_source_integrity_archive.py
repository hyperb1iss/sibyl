"""Archive completeness and typed evidence must survive JSON transport."""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.source_integrity import (
    build_integrity_archive,
    decode_record,
    encode_record,
    validate_integrity_archive,
)
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)


def archive():
    return build_integrity_archive(
        kind=SourceKind.RAW_CAPTURE,
        organizations=["org"],
        source_rows=[
            {
                "uuid": "memory",
                "organization_id": "org",
                "revision": 1,
                "raw_content": "signed bytes",
                "metadata": {"literal": "2026-09-09T00:00:00Z"},
                "created_at": datetime(2026, 9, 9, tzinfo=UTC),
            }
        ],
        source_states=[
            {
                "organization_id": "org",
                "source_kind": "raw_capture",
                "source_id": "memory",
                "generation": 9,
                "revision": 1,
                "deleted": False,
                "incarnation": "retained",
            },
            {
                "organization_id": "org",
                "source_kind": "raw_capture",
                "source_id": "deleted",
                "generation": 14,
                "revision": 8,
                "deleted": True,
                "incarnation": "tombstone",
            },
        ],
        derivations=[],
    )


def test_archive_preserves_typed_dates_and_literal_signed_metadata():
    payload = json.loads(json.dumps(archive()))
    rows, states, associations = validate_integrity_archive(
        payload, kind=SourceKind.RAW_CAPTURE, organizations=["org"]
    )
    assert rows[0]["created_at"] == datetime(2026, 9, 9, tzinfo=UTC)
    assert rows[0]["metadata"]["literal"] == "2026-09-09T00:00:00Z"
    assert states[1]["deleted"] is True
    assert states[1]["generation"] == 14
    assert not associations


@pytest.mark.parametrize("section", ["source_states", "derivations", "source_rows"])
def test_current_archive_rejects_omitted_collection(section):
    payload = archive()
    del payload[section]
    with pytest.raises(ValueError, match="incomplete"):
        validate_integrity_archive(payload, kind=SourceKind.RAW_CAPTURE, organizations=["org"])


def test_archive_rejects_tampered_ledger_before_restore():
    payload = deepcopy(archive())
    payload["source_states"][0]["generation"] = 1
    with pytest.raises(ValueError, match="digest"):
        validate_integrity_archive(payload, kind=SourceKind.RAW_CAPTURE, organizations=["org"])


def test_archive_scope_is_trusted_by_caller_not_payload():
    with pytest.raises(ValueError, match="organization"):
        validate_integrity_archive(archive(), kind=SourceKind.RAW_CAPTURE, organizations=["other"])


def test_typed_record_does_not_confuse_user_object_with_codec_marker():
    value = {"metadata": {"record": {"datetimes": []}}, "values": [None, False, 4, "2026-01-01"]}
    assert decode_record(json.loads(json.dumps(encode_record(value)))) == value


@pytest.mark.asyncio
async def test_source_export_reads_real_rows_and_retained_tombstones(runtime):
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.source_archive_store import export_source_integrity

    for identity in ("live-archive", "deleted-archive"):
        await runtime.entity_manager.create_direct(
            Entity(id=identity, entity_type=EntityType.SESSION, name=identity, content="Evidence")
        )
    await runtime.entity_manager.delete("deleted-archive")
    payload = await export_source_integrity(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    rows, states, _ = validate_integrity_archive(
        json.loads(json.dumps(payload)),
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    assert any(row["uuid"] == "live-archive" for row in rows)
    assert not any(row["uuid"] == "deleted-archive" for row in rows)
    assert next(state for state in states if state["source_id"] == "deleted-archive")["deleted"]


@pytest.fixture
async def destination(runtime):
    from uuid import uuid4

    from sibyl_core.backends.surreal.schema import bootstrap_schema
    from sibyl_core.services.graph_client import SurrealGraphClient
    from sibyl_core.services.graph_entities import EntityManager
    from sibyl_core.services.graph_relationships import RelationshipManager
    from sibyl_core.services.graph_runtime import GraphRuntime

    client = SurrealGraphClient(
        group_id=runtime.client.group_id, url="memory://", database="archive_" + uuid4().hex
    )
    try:
        await bootstrap_schema(client)
        yield GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=client.group_id),
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
    finally:
        await client.close()


async def test_complete_fresh_restore_preserves_protected_graph_ancestry(
    runtime, destination, content_store, monkeypatch
):
    from unittest.mock import AsyncMock

    from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
    from sibyl_core.services.source_archive_store import (
        export_source_integrity,
        restore_source_integrity,
    )
    from tests.test_memory_projection_derivations import protected_session

    source, target, _ = await protected_session(runtime, monkeypatch)
    payload = await export_source_integrity(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    result = await restore_source_integrity(
        destination.client.execute_query,
        json.loads(json.dumps(payload)),
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    assert not result["conflicts"]
    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
        AsyncMock(return_value=destination),
    )
    restored = await destination.entity_manager.get(target.id)
    assert restored.derivation_required
    assert target.id not in await unavailable_publication_ids(
        runtime.client.group_id, {target.id: restored.metadata}
    )
    await destination.entity_manager.update(source.id, {"content": "Changed evidence"})
    assert target.id in await unavailable_publication_ids(
        runtime.client.group_id, {target.id: restored.metadata}
    )


async def test_restore_cannot_revive_destination_source_tombstone(runtime, destination):
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.source_archive_store import (
        export_source_integrity,
        read_source_archive_snapshot,
        restore_source_integrity,
    )

    entity = Entity(
        id="tombstone", entity_type=EntityType.SESSION, name="Source", content="Original"
    )
    await runtime.entity_manager.create_direct(entity)
    payload = await export_source_integrity(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    await restore_source_integrity(
        destination.client.execute_query,
        payload,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    assert await destination.entity_manager.delete(entity.id)
    before = await read_source_archive_snapshot(
        destination.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    assert any(
        row["source_id"] == entity.id and row["deleted"] for row in before["source_states"]
    ), before
    result = await restore_source_integrity(
        destination.client.execute_query,
        payload,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
        clean=True,
        skip_existing=False,
    )
    assert any(row["reason"] == "retained_tombstone" for row in result["conflicts"])
    after = await read_source_archive_snapshot(
        destination.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    assert after == before


async def test_scoped_clean_keeps_other_org_and_retains_deleted_high_water(runtime, content_store):
    from sibyl_core.services import content_client
    from sibyl_core.services.source_archive_store import (
        export_source_integrity,
        read_source_archive_snapshot,
        restore_source_integrity,
    )
    from sibyl_core.services.surreal_content import remember_raw_memory

    async def remember(org, text):
        return await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=text,
            raw_content=text,
            embedding_provider=None,
        )

    org = runtime.client.group_id
    original = await remember(org, "Original")
    other = await remember("unrelated-org", "Unrelated")
    async with content_client.surreal_content_client() as client:
        payload = await export_source_integrity(
            client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=[org]
        )
        extra = await remember(org, "Added after archive")
        result = await restore_source_integrity(
            client.execute_query,
            payload,
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            clean=True,
        )
        assert not result["conflicts"]
        snapshot = await read_source_archive_snapshot(
            client.execute_query,
            kind=SourceKind.RAW_CAPTURE,
            organizations=None,
        )
        assert {row["uuid"] for row in snapshot["source_rows"]} == {original.id, other.id}
        tombstone = next(row for row in snapshot["source_states"] if row["source_id"] == extra.id)
        assert tombstone["deleted"] is True
        assert tombstone["generation"] >= 2


async def test_destination_mutation_between_snapshot_and_commit_aborts_restore(
    runtime, destination
):
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.source_archive_store import (
        export_source_integrity,
        restore_source_integrity,
    )

    entity = Entity(id="cas", entity_type=EntityType.SESSION, name="Source", content="Original")
    await runtime.entity_manager.create_direct(entity)
    payload = await export_source_integrity(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    original = destination.client.execute_query
    injected = False

    async def mutate_before_commit(statement, **params):
        nonlocal injected
        if statement.startswith("BEGIN TRANSACTION;") and not injected:
            injected = True
            await destination.entity_manager.create_direct(entity)
            await destination.entity_manager.update(entity.id, {"content": "Concurrent evidence"})
        return await original(statement, **params)

    with pytest.raises(Exception, match="destination changed"):
        await restore_source_integrity(
            mutate_before_commit,
            payload,
            kind=SourceKind.GRAPH_ENTITY,
            organizations=[runtime.client.group_id],
            clean=True,
            skip_existing=False,
        )
    assert injected
    assert (await destination.entity_manager.get(entity.id)).content == "Concurrent evidence"


async def test_restore_preserves_inactive_target_association(runtime, destination, monkeypatch):
    from sibyl_core.services.source_archive_store import (
        export_source_integrity,
        read_source_archive_snapshot,
        restore_source_integrity,
    )
    from tests.test_memory_projection_derivations import protected_session

    _, target, _ = await protected_session(runtime, monkeypatch)
    org = runtime.client.group_id
    payload = await export_source_integrity(
        runtime.client.execute_query, kind=SourceKind.GRAPH_ENTITY, organizations=[org]
    )
    await restore_source_integrity(
        destination.client.execute_query, payload, kind=SourceKind.GRAPH_ENTITY, organizations=[org]
    )
    assert await destination.entity_manager.delete(target.id)
    before = await read_source_archive_snapshot(
        destination.client.execute_query, kind=SourceKind.GRAPH_ENTITY, organizations=[org]
    )
    assert (
        next(row for row in before["derivations"] if row["target_id"] == target.id)["active"]
        is False
    )
    result = await restore_source_integrity(
        destination.client.execute_query,
        payload,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[org],
        clean=True,
        skip_existing=False,
    )
    assert any(
        row["source_id"] == target.id and row["reason"] == "retained_tombstone"
        for row in result["conflicts"]
    )
    after = await read_source_archive_snapshot(
        destination.client.execute_query, kind=SourceKind.GRAPH_ENTITY, organizations=[org]
    )
    assert after == before


async def test_foreign_physical_record_collision_rolls_back_scoped_cleanup(runtime, content_store):
    from sibyl_core.services import content_client
    from sibyl_core.services.source_archive_store import (
        export_source_integrity,
        read_source_archive_snapshot,
        restore_source_integrity,
    )
    from sibyl_core.services.surreal_content import remember_raw_memory

    org = runtime.client.group_id
    selected = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="selected",
        raw_content="Selected",
        embedding_provider=None,
    )
    other = await remember_raw_memory(
        organization_id="other-org",
        principal_id="owner",
        source_id="other",
        raw_content="Other",
        embedding_provider=None,
    )
    async with content_client.surreal_content_client() as client:
        payload = await export_source_integrity(
            client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=[org]
        )
        before = await read_source_archive_snapshot(
            client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=None
        )
        rows, states, associations = validate_integrity_archive(
            payload, kind=SourceKind.RAW_CAPTURE, organizations=[org]
        )
        foreign = next(row for row in before["source_rows"] if row["uuid"] == other.id)
        rows[0]["archive_record_key"] = foreign["archive_record_key"]
        rows[0]["uuid"] = "new-source-identity"
        states[0]["source_id"] = "new-source-identity"
        forged = build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            source_rows=rows,
            source_states=states,
            derivations=associations,
        )
        with pytest.raises(Exception, match="physical record identity conflicts"):
            await restore_source_integrity(
                client.execute_query,
                forged,
                kind=SourceKind.RAW_CAPTURE,
                organizations=[org],
                clean=True,
            )
        after = await read_source_archive_snapshot(
            client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=None
        )
        assert after == before
        assert selected.id in {row["uuid"] for row in after["source_rows"]}
