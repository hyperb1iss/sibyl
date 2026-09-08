"""A damaged derivative must not prevent correction of reachable descendants."""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import memory_lineage
from sibyl_core.services.content_models import RawMemory


@pytest.mark.parametrize("lane", ["graph", "raw"])
@pytest.mark.parametrize("damaged", ["malformed", "vanished", "private", "unversioned"])
async def test_correction_reports_damaged_row_and_still_retires_descendants(
    monkeypatch, lane, damaged
):
    root = RawMemory(
        id="root",
        organization_id="org",
        source_id="source",
        principal_id="owner",
        revision=2,
        metadata={"lifecycle_flags": ["hidden"]},
    )
    rows = {}
    for row_id in ("bad", "good", "grandchild"):
        owner = "someone-else" if row_id == "bad" and damaged == "private" else "owner"
        metadata = {"memory_scope": "private", "principal_id": owner}
        if row_id == "bad" and damaged == "malformed":
            metadata["correction_blockers"] = "malformed"
        if lane == "graph":
            rows[row_id] = Entity(
                id=row_id,
                name=row_id,
                entity_type=EntityType.EPISODE,
                metadata=metadata,
                observed_revision=None if row_id == "bad" and damaged == "unversioned" else 1,
            )
        else:
            rows[row_id] = RawMemory(
                id=row_id,
                organization_id="org",
                source_id=row_id,
                principal_id=owner,
                revision=1,
                observed_revision=None if row_id == "bad" and damaged == "unversioned" else 1,
                metadata=metadata,
            )
    if damaged == "vanished":
        rows["bad"] = None

    @asynccontextmanager
    async def session():
        yield object()

    async def descendants(_runtime, *, raw_ids, graph_ids, **_kwargs):
        if lane == "graph":
            if "root" in raw_ids:
                yield "bad"
                yield "good"
            if "bad" in graph_ids:
                yield "grandchild"

    async def select(_client, _query, *, source_ids, **_kwargs):
        if lane == "raw":
            if "root" in source_ids:
                return [{"uuid": "bad"}, {"uuid": "good"}]
            if "bad" in source_ids:
                return [{"uuid": "grandchild"}]
        return []

    def graph_get(entity_id):
        row = rows[entity_id]
        if row is None:
            raise KeyError(entity_id)
        return row

    manager = SimpleNamespace(
        get=AsyncMock(side_effect=graph_get),
        update=AsyncMock(side_effect=lambda entity_id, *_args, **_kwargs: rows[entity_id]),
    )
    raw_save = AsyncMock(side_effect=lambda row, **_kwargs: row)
    monkeypatch.setattr(memory_lineage.content_client, "surreal_content_client", session)
    monkeypatch.setattr(memory_lineage.content_client, "select_many", select)
    monkeypatch.setattr(memory_lineage, "_graph_descendant_ids", descendants)
    monkeypatch.setattr(
        memory_lineage,
        "get_raw_memory",
        AsyncMock(side_effect=lambda *, memory_id, **_kwargs: rows[memory_id]),
    )
    monkeypatch.setattr(memory_lineage, "save_raw_memory", raw_save)
    receipt = await memory_lineage.propagate_source_correction(
        SimpleNamespace(entity_manager=manager),
        memory=root,
        principal_id="owner",
        accessible_projects=set(),
    )
    assert receipt.complete is (damaged == "private")
    expected = ["good", "grandchild"]
    written = ["bad", *expected] if damaged == "private" else expected
    if lane == "graph":
        assert receipt.entity_ids == expected
        assert receipt.raw_memory_ids == []
        assert [call.args[0] for call in manager.update.await_args_list] == written
        for call in manager.update.await_args_list:
            assert call.args[1]["metadata"]["correction_blockers"]["root"]["blocking"] is True
        raw_save.assert_not_awaited()
    else:
        assert receipt.raw_memory_ids == expected
        assert receipt.entity_ids == []
        assert [call.args[0].id for call in raw_save.await_args_list] == written
        for call in raw_save.await_args_list:
            assert call.args[0].metadata["correction_blockers"]["root"]["blocking"] is True
        manager.update.assert_not_awaited()


@pytest.mark.parametrize("failed_lane", ["raw", "graph"])
async def test_lookup_failure_preserves_other_graph_arms(monkeypatch, failed_lane):
    root = RawMemory(
        id="root",
        organization_id="org",
        source_id="source",
        principal_id="owner",
        revision=2,
        observed_revision=2,
        metadata={"lifecycle_flags": ["hidden"]},
    )
    rows = {
        row_id: Entity(
            id=row_id,
            name=row_id,
            entity_type=EntityType.EPISODE,
            observed_revision=1,
            metadata={"memory_scope": "private", "principal_id": "owner"},
        )
        for row_id in ("early", "late")
    }

    @asynccontextmanager
    async def session():
        yield object()

    async def select(*_args, **_kwargs):
        if failed_lane == "raw":
            raise RuntimeError("Unavailable raw lineage index")
        return []

    async def query(statement, **_kwargs):
        if "idx_entity_raw_sources" in statement:
            return [{"uuid": "early"}]
        if "idx_entity_review_capture" in statement and failed_lane == "graph":
            raise RuntimeError("Unavailable review lineage index")
        if "idx_entity_raw_memory" in statement:
            return [{"uuid": "late"}]
        return []

    manager = SimpleNamespace(
        get=AsyncMock(side_effect=lambda entity_id: rows[entity_id]),
        update=AsyncMock(side_effect=lambda entity_id, *_args, **_kwargs: rows[entity_id]),
    )
    monkeypatch.setattr(memory_lineage.content_client, "surreal_content_client", session)
    monkeypatch.setattr(memory_lineage.content_client, "select_many", select)
    receipt = await memory_lineage.propagate_source_correction(
        SimpleNamespace(entity_manager=manager, client=SimpleNamespace(execute_query=query)),
        memory=root,
        principal_id="owner",
        accessible_projects=set(),
    )
    assert not receipt.complete
    assert receipt.entity_ids == ["early", "late"]
    assert [call.args[0] for call in manager.update.await_args_list] == ["early", "late"]


@pytest.mark.parametrize("lane", ["graph", "raw"])
async def test_lookup_failure_does_not_discard_later_source_batches(monkeypatch, lane):
    batch_size = memory_lineage.content_client.DEFAULT_BATCH_SIZE
    source_ids = [f"source-{index}" for index in range(batch_size + 1)]
    seen_batches = []

    @asynccontextmanager
    async def session():
        yield object()

    async def query(*_args, source_ids, **_kwargs):
        seen_batches.append(source_ids)
        if "source-0" in source_ids:
            raise RuntimeError("The first index batch is unavailable")
        return [{"uuid": "survivor"}]

    failures = set()
    if lane == "raw":
        monkeypatch.setattr(memory_lineage.content_client, "surreal_content_client", session)
        monkeypatch.setattr(memory_lineage.content_client, "select_many", query)
        descendants = memory_lineage._raw_descendant_ids(
            organization_id="org", source_ids=source_ids, lookup_failures=failures
        )
    else:
        descendants = memory_lineage._graph_descendant_ids(
            SimpleNamespace(client=SimpleNamespace(execute_query=query)),
            organization_id="org",
            raw_ids=source_ids,
            graph_ids=[],
            lookup_failures=failures,
        )
    found = [row_id async for row_id in descendants]
    assert failures
    assert "survivor" in found
    assert all(len(batch) <= batch_size for batch in seen_batches)
    assert set().union(*(set(batch) for batch in seen_batches)) == set(source_ids)


@pytest.mark.parametrize("lane", ["graph", "raw"])
@pytest.mark.parametrize("other_writer_applied_root", [False, True])
async def test_conflicting_correction_merges_fresh_state(
    monkeypatch, lane, other_writer_applied_root
):
    root = RawMemory(
        id="root",
        organization_id="org",
        source_id="source",
        principal_id="owner",
        revision=2,
        observed_revision=2,
        metadata={"lifecycle_flags": ["hidden"]},
    )
    original = {
        "memory_scope": "private",
        "principal_id": "owner",
        "correction_blockers": {"other-root": {"revision": 3, "blocking": False}},
    }
    fresh = deepcopy(original)
    fresh["correction_blockers"]["other-root"] = {"revision": 4, "blocking": True}
    fresh["authored_marker"] = "concurrent edit"
    if other_writer_applied_root:
        fresh["correction_blockers"]["root"] = {"revision": 2, "blocking": True}
    saved = {"metadata": original, "revision": 1}
    attempts = []

    def row():
        if lane == "raw":
            return RawMemory(
                id="child",
                organization_id="org",
                source_id="child",
                principal_id="owner",
                revision=saved["revision"],
                observed_revision=saved["revision"],
                metadata=deepcopy(saved["metadata"]),
            )
        return Entity(
            id="child",
            name="child",
            entity_type=EntityType.EPISODE,
            observed_revision=saved["revision"],
            metadata=deepcopy(saved["metadata"]),
        )

    def write(metadata, expected_revision):
        attempts.append(expected_revision)
        if len(attempts) == 1:
            saved.update(metadata=fresh, revision=2)
            raise RevisionConflictError("child", expected_revision, 2)
        assert expected_revision == saved["revision"]
        saved["metadata"].update(metadata)
        saved["revision"] += 1
        return row()

    async def graph_update(_id, fields, *, expected_revision, **_kwargs):
        return write(fields["metadata"], expected_revision)

    async def raw_save(value, *, expected_revision):
        return write(value.metadata, expected_revision)

    async def raw_descendants(*, source_ids, **_kwargs):
        if lane == "raw" and "root" in source_ids:
            yield "child"

    async def graph_descendants(_runtime, *, raw_ids, **_kwargs):
        if lane == "graph" and "root" in raw_ids:
            yield "child"

    monkeypatch.setattr(memory_lineage, "_raw_descendant_ids", raw_descendants)
    monkeypatch.setattr(memory_lineage, "_graph_descendant_ids", graph_descendants)
    monkeypatch.setattr(memory_lineage, "get_raw_memory", AsyncMock(side_effect=lambda **_: row()))
    monkeypatch.setattr(memory_lineage, "save_raw_memory", raw_save)
    manager = SimpleNamespace(get=AsyncMock(side_effect=lambda _: row()), update=graph_update)
    receipt = await memory_lineage.propagate_source_correction(
        SimpleNamespace(entity_manager=manager),
        memory=root,
        principal_id="owner",
        accessible_projects=set(),
    )
    assert receipt.complete
    assert (receipt.raw_memory_ids if lane == "raw" else receipt.entity_ids) == ["child"]
    assert attempts == ([1] if other_writer_applied_root else [1, 2])
    assert saved["metadata"]["authored_marker"] == "concurrent edit"
    assert saved["metadata"]["correction_blockers"] == {
        "root": {"revision": 2, "blocking": True},
        "other-root": {"revision": 4, "blocking": True},
    }


@pytest.mark.parametrize("lane", ["graph", "raw"])
@pytest.mark.parametrize("grants", [None, frozenset(), frozenset({"project\x1fproject-a"})])
async def test_credential_scope_filters_receipt_without_skipping_correction(
    monkeypatch, lane, grants
):
    root = RawMemory(
        id="root",
        organization_id="org",
        source_id="source",
        principal_id="owner",
        revision=2,
        observed_revision=2,
        metadata={"lifecycle_flags": ["hidden"]},
    )
    metadata = {"memory_scope": "private", "principal_id": "owner"}
    child = (
        RawMemory(
            id="child",
            organization_id="org",
            source_id="child",
            principal_id="owner",
            revision=1,
            observed_revision=1,
            metadata=metadata,
        )
        if lane == "raw"
        else Entity(
            id="child",
            name="child",
            entity_type=EntityType.EPISODE,
            observed_revision=1,
            metadata=metadata,
        )
    )

    async def raw_descendants(*, source_ids, **_kwargs):
        if lane == "raw" and "root" in source_ids:
            yield "child"

    async def graph_descendants(_runtime, *, raw_ids, **_kwargs):
        if lane == "graph" and "root" in raw_ids:
            yield "child"

    manager = SimpleNamespace(
        get=AsyncMock(return_value=child), update=AsyncMock(return_value=child)
    )
    save = AsyncMock(return_value=child)
    monkeypatch.setattr(memory_lineage, "_raw_descendant_ids", raw_descendants)
    monkeypatch.setattr(memory_lineage, "_graph_descendant_ids", graph_descendants)
    monkeypatch.setattr(memory_lineage, "get_raw_memory", AsyncMock(return_value=child))
    monkeypatch.setattr(memory_lineage, "save_raw_memory", save)
    receipt = await memory_lineage.propagate_source_correction(
        SimpleNamespace(entity_manager=manager),
        memory=root,
        principal_id="owner",
        accessible_projects={"project-a"},
        allowed_memory_scope_keys=grants,
    )
    assert receipt.complete
    assert (receipt.raw_memory_ids if lane == "raw" else receipt.entity_ids) == (
        ["child"] if grants is None else []
    )
    written = (
        save.await_args.args[0].metadata
        if lane == "raw"
        else manager.update.await_args.args[1]["metadata"]
    )
    assert written["correction_blockers"]["root"] == {"revision": 2, "blocking": True}
