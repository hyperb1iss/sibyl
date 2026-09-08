"""Interrupted v2 reservations retain their identity across v3 promotion."""

import hashlib
import json
from dataclasses import replace

import pytest

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.services import memory_reflection
from sibyl_core.services.memory_identity import verify_reflection_identity
from sibyl_core.services.memory_promotion import _entity_from_candidate
from sibyl_core.services.surreal_content import (
    get_raw_memory,
    remember_raw_memory,
    remember_reflection_candidate_review,
    save_raw_memory,
)
from tests.test_reflection_identity import candidate
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def legacy_input(runtime, mode="raw", title="Reserved evidence"):
    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="upgrade-source",
        title=title,
        raw_content="The original candidate remains immutable.",
        embedding_provider=None,
    )
    if mode == "review":
        raw = await remember_reflection_candidate_review(
            organization_id=raw.organization_id,
            principal_id="user_a",
            candidate=replace(candidate(), title=title),
            raw_source_ids=[raw.id],
        )
    args = dict(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        promote_to_scope="private",
    )
    if mode == "review":
        args["candidate_id"] = raw.id
        plan = await memory_reflection._resolve_reflection_promotion_plan(**args)
    else:
        args["raw_memory_id"] = raw.id
        plan = await memory_reflection._resolve_raw_memory_promotion_plan(**args)
    legacy = _entity_from_candidate(
        plan.promotion_candidate,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        domain=None,
        project=plan.target_project,
        source_id=plan.raw_source_ids[0],
        memory_scope=plan.target_scope,
        scope_key=plan.target_scope_key,
        policy_metadata={},
    )
    legacy.metadata.pop("source_bindings", None)
    # Reconstruct the shipped v2 payload independently of the current identity
    # helpers and builder's title normalization.
    legacy.name = title
    fields = legacy.metadata
    identity = {
        "version": 2,
        "purpose": "source" if fields.get("reflection_source") is True else "candidate",
        "kind": legacy.entity_type.value,
        "title": title.strip(),
        "content_sha256": hashlib.sha256(legacy.content.encode()).hexdigest(),
        "organization_id": legacy.organization_id,
        "principal_id": fields.get("principal_id"),
        "created_by": legacy.created_by,
        "memory_scope": fields.get("memory_scope"),
        "scope_key": fields.get("scope_key"),
        "project_id": fields.get("project_id"),
        "domain": fields.get("category") or None,
        "primary_source_id": legacy.source_file,
        "source_ids": sorted(
            {
                *fields.get("raw_source_ids", []),
                *fields.get("source_ids", []),
                *([legacy.source_file] if legacy.source_file else []),
            }
        ),
        "review_capture_id": fields.get("review_capture_id"),
        "imported_capture_id": fields.get("imported_capture_id"),
    }
    identity = {key: value for key, value in identity.items() if value is not None}
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    legacy.id = f"{legacy.entity_type.value}_v2_{hashlib.sha256(payload).hexdigest()}"
    legacy.metadata["reflection_identity"] = identity
    assert "_v2_" in legacy.id
    return raw, legacy, args


async def publish(args):
    if "candidate_id" in args:
        return await memory_reflection.promote_reflection_candidate_review(**args)
    return await memory_reflection.promote_raw_memory(**args)


async def reserve_legacy(raw, legacy):
    return await save_raw_memory(
        replace(
            raw,
            metadata={
                **raw.metadata,
                "promoted_entity_id": legacy.id,
                "promotion_state": "pending",
            },
        ),
        expected_revision=raw.revision,
        embedding_provider=None,
    )


@pytest.mark.parametrize("mode", ["raw", "review"])
@pytest.mark.parametrize("old_first", [False, True])
@pytest.mark.parametrize("title", ["Reserved evidence", "", " \t\n "])
async def test_v2_reservation_old_and_new_publishers_converge(
    runtime, content_store, old_first, mode, title, monkeypatch
):
    raw, legacy, args = await legacy_input(runtime, mode, title)
    await reserve_legacy(raw, legacy)
    if old_first:
        await runtime.entity_manager.create_direct_if_absent(legacy)
    real_insert = runtime.entity_manager.create_direct_if_absent

    async def checked_insert(entity):
        assert entity.id == legacy.id
        assert not graph_metadata_recallable(entity.metadata)
        assert all(value == 0 for value in entity.metadata["source_bindings"].values())
        return await real_insert(entity)

    monkeypatch.setattr(runtime.entity_manager, "create_direct_if_absent", checked_insert)
    result = await publish(args)
    monkeypatch.setattr(runtime.entity_manager, "create_direct_if_absent", real_insert)
    assert result.success
    assert result.promoted_id == legacy.id
    stored, created = await runtime.entity_manager.create_direct_if_absent(legacy)
    assert not created
    verify_reflection_identity(legacy, stored)
    if not title.strip():
        assert stored.name == ""
        assert stored.metadata["reflection_identity"]["title"] == ""
        with pytest.raises(ValueError, match="identity conflict"):
            verify_reflection_identity(legacy, stored.model_copy(update={"name": "renamed"}))
        with pytest.raises(ValueError, match="identity conflict"):
            verify_reflection_identity(legacy, stored.model_copy(update={"content": "changed"}))
    assert "source_snapshot_sha256" not in stored.metadata
    bindings = stored.metadata.get("source_bindings", {})
    assert all(value == 0 for value in bindings.values())
    saved = await get_raw_memory(organization_id=raw.organization_id, memory_id=raw.id)
    assert saved.metadata["promoted_entity_id"] == legacy.id
    rows = await runtime.client.execute_query(
        "SELECT VALUE uuid FROM entity WHERE entity_type != 'project';"
    )
    assert rows == [legacy.id]
    replay = await publish(args)
    assert replay.success and replay.promoted_id == legacy.id


@pytest.mark.parametrize("title", ["Reserved evidence", "", " \t "])
async def test_v2_reservation_winning_cas_is_resumed(runtime, content_store, monkeypatch, title):
    raw, legacy, args = await legacy_input(runtime, title=title)
    real_save = memory_reflection.save_raw_memory
    intercepted = False

    async def racing_save(memory, **kwargs):
        nonlocal intercepted
        if not intercepted and memory.metadata.get("promotion_state") == "pending":
            intercepted = True
            await reserve_legacy(raw, legacy)
        return await real_save(memory, **kwargs)

    monkeypatch.setattr(memory_reflection, "save_raw_memory", racing_save)
    result = await publish(args)
    assert intercepted
    assert result.success and result.promoted_id == legacy.id


@pytest.mark.parametrize("existing", [False, True])
async def test_v2_replay_never_binds_to_corrected_source(runtime, content_store, existing):
    raw, legacy, args = await legacy_input(runtime)
    reserved = await reserve_legacy(raw, legacy)
    if existing:
        await runtime.entity_manager.create_direct_if_absent(legacy)
    await save_raw_memory(
        replace(
            reserved,
            metadata={
                **reserved.metadata,
                "correction_history": [{"action": "revise", "prior_revision": reserved.revision}],
            },
        ),
        expected_revision=reserved.revision,
        embedding_provider=None,
    )
    result = await publish(args)
    assert not result.success and result.reason == "retired"
    stored = await runtime.entity_manager.get(legacy.id)
    assert not graph_metadata_recallable(stored.metadata)
    assert "source_snapshot_sha256" not in stored.metadata
    assert stored.metadata.get("source_bindings", {}).get(raw.id, 0) == 0


@pytest.mark.parametrize("retired", [False, True])
async def test_v2_existing_bindings_and_own_retirement_survive(runtime, content_store, retired):
    raw, legacy, args = await legacy_input(runtime)
    await reserve_legacy(raw, legacy)
    legacy.metadata["source_bindings"] = {raw.id: raw.revision}
    if retired:
        legacy.metadata["excluded_from_recall"] = True
    await runtime.entity_manager.create_direct_if_absent(legacy)
    result = await publish(args)
    assert result.success is not retired
    stored = await runtime.entity_manager.get(legacy.id)
    assert stored.metadata["source_bindings"] == {raw.id: raw.revision}
    assert graph_metadata_recallable(stored.metadata) is not retired
    assert "source_snapshot_sha256" not in stored.metadata


@pytest.mark.parametrize("change", ["domain", "content", "pointer"])
async def test_v2_reservation_requires_exact_reconstruction(runtime, content_store, change):
    raw, legacy, args = await legacy_input(runtime)
    reserved = await reserve_legacy(raw, legacy)
    if change == "domain":
        args["domain"] = "different-domain"
    elif change == "content":
        await save_raw_memory(
            replace(reserved, raw_content="Different output."),
            expected_revision=reserved.revision,
            embedding_provider=None,
        )
    else:
        await save_raw_memory(
            replace(
                reserved,
                metadata={
                    **reserved.metadata,
                    "promoted_entity_id": "note_v2_not-a-canonical-identity",
                },
            ),
            expected_revision=reserved.revision,
            embedding_provider=None,
        )
    before = await get_raw_memory(organization_id=raw.organization_id, memory_id=raw.id)
    result = await publish(args)
    assert not result.success and result.reason == "candidate_already_promoted"
    assert await get_raw_memory(organization_id=raw.organization_id, memory_id=raw.id) == before
    assert (
        await runtime.client.execute_query(
            "SELECT VALUE uuid FROM entity WHERE entity_type != 'project';"
        )
        == []
    )


@pytest.mark.parametrize("title", ["Reserved evidence", "", " \t "])
async def test_unreserved_promotion_uses_v3(runtime, content_store, title):
    raw, legacy, args = await legacy_input(runtime, title=title)
    result = await publish(args)
    assert result.success and "_v3_" in result.promoted_id
    assert result.promoted_id != legacy.id
    stored = await runtime.entity_manager.get(result.promoted_id)
    assert stored.name == (title.strip() or "Untitled memory")
    assert stored.metadata["source_snapshot_sha256"]
    assert stored.metadata["source_bindings"][raw.id] > 0


async def test_v2_partial_publication_resumes_its_existing_receipt(
    runtime, content_store, monkeypatch
):
    raw, legacy, args = await legacy_input(runtime)
    await reserve_legacy(raw, legacy)
    real_relationships = runtime.relationship_manager.create_bulk
    args["related_to"] = ["project_a"]
    args["accessible_projects"] = {"project_a"}

    async def unavailable(relationships):
        return 0, len(relationships)

    monkeypatch.setattr(runtime.relationship_manager, "create_bulk", unavailable)
    first = await publish(args)
    assert not first.success and first.reason == "promotion_incomplete"
    pending = await get_raw_memory(organization_id=raw.organization_id, memory_id=raw.id)
    assert pending.metadata["promoted_entity_id"] == legacy.id
    assert pending.metadata["promotion_state"] == "pending"
    monkeypatch.setattr(runtime.relationship_manager, "create_bulk", real_relationships)
    resumed = await publish(args)
    assert resumed.success and resumed.promoted_id == legacy.id
    assert resumed.metadata["publication_outcome"] == "resumed"
    complete = await runtime.entity_manager.get(legacy.id)
    replayed = await publish(args)
    assert replayed.success and replayed.metadata["publication_outcome"] == "replayed"
    stored = await runtime.entity_manager.get(legacy.id)
    assert stored.metadata["reflection_publication"] == complete.metadata["reflection_publication"]
    assert stored.metadata["source_bindings"] == complete.metadata["source_bindings"]


@pytest.mark.parametrize("title", ["", " \t "])
async def test_literal_uuid_title_is_not_proof_of_blank_storage(runtime, content_store, title):
    _, legacy, _ = await legacy_input(runtime, title=title)
    await runtime.entity_manager.create_direct_if_absent(legacy)
    original = await runtime.entity_manager.get(legacy.id)
    assert original.name == ""
    verify_reflection_identity(legacy, original)
    await runtime.client.execute_query(
        "UPDATE entity SET name = $name WHERE uuid = $uuid;", name=legacy.id, uuid=legacy.id
    )
    rows = await runtime.client.execute_query(
        "SELECT name FROM entity WHERE uuid = $uuid;", uuid=legacy.id
    )
    assert rows == [{"name": legacy.id}]
    stored = await runtime.entity_manager.get(legacy.id)
    assert stored.metadata["reflection_identity"] == original.metadata["reflection_identity"]
    assert stored.name == legacy.id
    with pytest.raises(ValueError, match="identity conflict"):
        verify_reflection_identity(legacy, stored)


def test_unversioned_graph_names_retain_display_fallback():
    from sibyl_core.services.graph_records import entity_from_surreal_row

    row = {"uuid": "ordinary", "entity_type": "note", "name": "", "metadata": {}}
    assert entity_from_surreal_row(row).name == "ordinary"
