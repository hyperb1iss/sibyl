"""Raw capture listing hides a raw memory once its projection row exists."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from sibyl.persistence.content_common import RawCaptureRecord
from sibyl.persistence.surreal import content as surreal_content
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_RAW_CAPTURE_PROJECTION_FOLD_BACKFILL,
)
from sibyl_core.backends.surreal.schema_capture_project import (
    file_capture_sidecars_under_project,
)


@pytest_asyncio.fixture
async def content_client(monkeypatch: pytest.MonkeyPatch):
    """An embedded store, or a scratch namespace on a live server.

    The embedded engine accepts statements a 3.x server rejects, so set
    SIBYL_LIVE_SURREAL_TESTS=1 and SIBYL_SURREAL_URL to run every listing and
    backfill statement here against a real server.
    """
    await surreal_content.close_shared_surreal_content_client()
    namespace: str | None = None
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") == "1":
        namespace = f"raw_capture_listing_{uuid4().hex}"
        client = SurrealContentClient(
            url=os.environ["SIBYL_SURREAL_URL"],
            username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            namespace=namespace,
        )
        await bootstrap_content_schema(client)
    else:
        client = SurrealContentClient(url="memory://")
        await bootstrap_content_schema(client, reset=True)

    @asynccontextmanager
    async def scope():
        yield client

    monkeypatch.setattr(surreal_content, "surreal_content_client", scope)
    try:
        yield client
    finally:
        await surreal_content.close_shared_surreal_content_client()
        if namespace is not None:
            await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
        await client.close()


def _capture(
    organization_id: UUID,
    *,
    title: str = "zsh pipeline trap",
    entity_type: str = "raw_memory",
    entity_id: str | None = None,
    review_state: str = "pending",
    metadata: dict[str, object] | None = None,
    principal_id: str = "user-1",
) -> RawCaptureRecord:
    return RawCaptureRecord(
        organization_id=organization_id,
        title=title,
        raw_content="paste mid-pipeline swallows stray stdin",
        entity_type=entity_type,
        entity_id=entity_id,
        review_state=review_state,
        metadata=dict(metadata or {}),
        principal_id=principal_id,
    )


async def _save(*records: RawCaptureRecord) -> None:
    for record in records:
        await surreal_content.save_raw_capture_record(None, capture=record)


async def _list(
    organization_id: UUID,
    *,
    entity_type: str | None = None,
    review_state: str | None = None,
) -> tuple[list[RawCaptureRecord], bool]:
    return await surreal_content.list_raw_captures(
        None,
        organization_id=organization_id,
        entity_type=entity_type,
        capture_surface=None,
        review_state=review_state,
        limit=10,
        offset=0,
    )


async def _stamp(
    org: UUID,
    raw: RawCaptureRecord,
    projected: RawCaptureRecord,
    *,
    principal_id: str = "user-1",
) -> bool:
    return await surreal_content.mark_raw_capture_projected(
        None,
        organization_id=org,
        raw_capture_id=str(raw.id),
        projected_capture_id=projected.id,
        principal_id=principal_id,
    )


@pytest.mark.asyncio
async def test_stamped_raw_memory_folds_into_its_projection(content_client) -> None:
    org = uuid4()
    raw = _capture(org)
    projected = _capture(
        org,
        entity_type="error_pattern",
        entity_id="error_pattern_abc123",
        metadata={"raw_memory_id": str(raw.id)},
    )
    unprojected = _capture(org, title="still waiting on projection")
    await _save(raw, projected, unprojected)

    assert await _stamp(org, raw, projected) is True

    captures, has_more = await _list(org)
    listed = {capture.id for capture in captures}
    assert projected.id in listed
    assert unprojected.id in listed
    assert raw.id not in listed
    assert has_more is False


@pytest.mark.asyncio
async def test_stamp_is_idempotent_and_organization_scoped(content_client) -> None:
    org = uuid4()
    raw = _capture(org)
    projected = _capture(org, entity_type="decision", entity_id="decision_1234")
    await _save(raw, projected)

    assert await _stamp(org, raw, projected) is True
    assert await _stamp(org, raw, projected) is False

    stranger = _capture(uuid4(), entity_type="decision", entity_id="decision_ffff")
    other_raw = _capture(org, title="untouched")
    await _save(stranger, other_raw)
    assert await _stamp(stranger.organization_id, other_raw, stranger) is False

    stored = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=raw.id)
    assert stored is not None
    assert stored.metadata["projected_capture_id"] == str(projected.id)
    listed, _ = await _list(org)
    assert {capture.id for capture in listed} == {other_raw.id, projected.id}


@pytest.mark.asyncio
async def test_stamp_is_bound_to_the_raw_row_principal(content_client) -> None:
    org = uuid4()
    raw = _capture(org, principal_id="owner")
    projected = _capture(org, entity_type="decision", entity_id="decision_1234")
    await _save(raw, projected)

    assert await _stamp(org, raw, projected, principal_id="someone-else") is False
    assert await _stamp(org, raw, projected, principal_id="") is False
    listed, _ = await _list(org)
    assert {capture.id for capture in listed} == {raw.id, projected.id}

    assert await _stamp(org, raw, projected, principal_id="owner") is True
    listed, _ = await _list(org)
    assert {capture.id for capture in listed} == {projected.id}


@pytest.mark.asyncio
async def test_stamp_ignores_projection_rows(content_client) -> None:
    org = uuid4()
    projected = _capture(org, entity_type="decision", entity_id="decision_1234")
    other = _capture(org, entity_type="episode", entity_id="episode_9999")
    await _save(projected, other)

    assert await _stamp(org, projected, other) is False


@pytest.mark.asyncio
async def test_fold_composes_with_type_and_review_filters(content_client) -> None:
    org = uuid4()
    raw = _capture(org)
    projected = _capture(org, entity_type="decision", entity_id="decision_1234")
    deferred = _capture(org, title="deferred raw", review_state="deferred")
    await _save(raw, projected, deferred)
    await _stamp(org, raw, projected)

    only_raw, _ = await _list(org, entity_type="raw_memory")
    assert [capture.id for capture in only_raw] == [deferred.id]

    pending, _ = await _list(org, review_state="pending")
    assert [capture.id for capture in pending] == [projected.id]


@pytest.mark.asyncio
async def test_projection_fold_backfill_ignores_foreign_projections(content_client) -> None:
    """A projection may not hide a raw row in another org or another principal."""
    org = uuid4()
    victim = _capture(org, title="mine", principal_id="owner")
    foreign_org_projection = _capture(
        uuid4(),
        entity_type="decision",
        entity_id="decision_ffff",
        metadata={"raw_memory_id": str(victim.id)},
        principal_id="owner",
    )
    other_principal_projection = _capture(
        org,
        entity_type="decision",
        entity_id="decision_eeee",
        metadata={"raw_memory_id": str(victim.id)},
        principal_id="someone-else",
    )
    await _save(victim, foreign_org_projection, other_principal_projection)

    await content_client.execute_query(CONTENT_RAW_CAPTURE_PROJECTION_FOLD_BACKFILL)

    stored = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=victim.id)
    assert stored is not None
    assert "projected_capture_id" not in stored.metadata
    listed, _ = await _list(org)
    assert victim.id in {capture.id for capture in listed}


@pytest.mark.asyncio
async def test_projection_fold_backfill_stamps_legacy_pairs(content_client) -> None:
    org = uuid4()
    raw = _capture(org)
    projected = _capture(
        org,
        entity_type="error_pattern",
        entity_id="error_pattern_abc123",
        metadata={"raw_memory_id": str(raw.id)},
    )
    lone = _capture(org, title="never projected")
    foreign_raw = _capture(uuid4(), title="another org, unrelated")
    await _save(raw, projected, lone, foreign_raw)

    before, _ = await _list(org)
    assert {capture.id for capture in before} == {raw.id, projected.id, lone.id}

    await content_client.execute_query(CONTENT_RAW_CAPTURE_PROJECTION_FOLD_BACKFILL)

    after, _ = await _list(org)
    assert {capture.id for capture in after} == {projected.id, lone.id}
    stamped = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=raw.id)
    assert stamped is not None
    assert stamped.metadata["projected_capture_id"] == str(projected.id)
    untouched = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=lone.id)
    assert untouched is not None
    assert "projected_capture_id" not in untouched.metadata


@pytest.mark.asyncio
async def test_mcp_link_marks_the_raw_memory_without_hiding_it_from_review(
    content_client,
) -> None:
    """The link names the graph row; the review list still shows the only capture."""
    org = uuid4()
    raw = _capture(org)
    await _save(raw)

    async def link(principal_id: str = "user-1") -> bool:
        return await surreal_content.mark_raw_memory_projected_entity(
            None,
            organization_id=org,
            raw_capture_id=str(raw.id),
            entity_id="decision_abc123",
            principal_id=principal_id,
        )

    # Bound to the writer: another principal cannot link someone's memory.
    assert await link("user-2") is False
    assert await link() is True
    assert await link() is False

    stored = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=raw.id)
    assert stored is not None
    assert stored.metadata["projected_entity_id"] == "decision_abc123"
    captures, _ = await _list(org)
    assert raw.id in {capture.id for capture in captures}


@pytest.mark.asyncio
async def test_private_owner_filter_keeps_foreign_private_rows_out_of_the_page(
    content_client,
) -> None:
    """A page of other principals' private rows must not starve the reader's own."""
    org = uuid4()
    base = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    mine = replace(_capture(org, title="mine", principal_id="reader"), created_at=base)
    foreign = [
        replace(
            _capture(org, title=f"theirs {minute}", principal_id="someone-else"),
            created_at=base + timedelta(minutes=minute),
        )
        for minute in range(1, 4)
    ]
    unstamped = replace(
        _capture(org, title="legacy, no principal", principal_id=""),
        created_at=base + timedelta(minutes=5),
    )
    shared = replace(
        _capture(org, title="project row", principal_id="someone-else"),
        memory_scope="project",
        scope_key="project_1",
        created_at=base + timedelta(minutes=6),
    )
    await _save(mine, *foreign, unstamped, shared)

    unfiltered, has_more = await surreal_content.list_raw_captures(
        None,
        organization_id=org,
        entity_type=None,
        capture_surface=None,
        review_state=None,
        limit=3,
        offset=0,
    )
    assert mine.id not in {capture.id for capture in unfiltered}
    assert has_more is True

    page, has_more = await surreal_content.list_raw_captures(
        None,
        organization_id=org,
        entity_type=None,
        capture_surface=None,
        review_state=None,
        private_owner="reader",
        limit=3,
        offset=0,
    )
    assert [capture.id for capture in page] == [shared.id, unstamped.id, mine.id]
    assert has_more is False


def _sidecar(
    organization_id: UUID,
    *,
    principal_id: str = "user-1",
    project_id: str | None = None,
) -> RawCaptureRecord:
    return replace(
        _capture(
            organization_id,
            entity_type="decision",
            entity_id=f"decision_{uuid4().hex[:12]}",
            principal_id=principal_id,
        ),
        project_id=project_id,
    )


def _raw_memory(
    organization_id: UUID,
    *,
    principal_id: str = "user-1",
    memory_scope: str = "private",
    scope_key: str | None = None,
    project_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> RawCaptureRecord:
    return replace(
        _capture(organization_id, principal_id=principal_id, metadata=metadata),
        memory_scope=memory_scope,
        scope_key=scope_key,
        project_id=project_id,
    )


async def _stored(org: UUID, capture: RawCaptureRecord) -> RawCaptureRecord:
    stored = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=capture.id)
    assert stored is not None
    return stored


@pytest.mark.asyncio
async def test_sidecar_backfill_files_sidecars_under_their_project(content_client) -> None:
    org = uuid4()
    # The CLI path: a private memory whose project lives only in raw metadata.
    private_raw = _raw_memory(org, metadata={"project_id": "project_1"})
    private_sidecar = _sidecar(org)
    # A project-scoped memory names its project as its scope key.
    project_raw = _raw_memory(org, memory_scope="project", scope_key="project_2")
    project_sidecar = _sidecar(org)
    # Nothing to file: the memory has no project.
    plain_raw = _raw_memory(org)
    plain_sidecar = _sidecar(org)
    await _save(
        private_raw, private_sidecar, project_raw, project_sidecar, plain_raw, plain_sidecar
    )
    for raw, sidecar in (
        (private_raw, private_sidecar),
        (project_raw, project_sidecar),
        (plain_raw, plain_sidecar),
    ):
        assert await _stamp(org, raw, sidecar) is True

    async def listed(project_id: str) -> list[UUID]:
        captures, _ = await surreal_content.list_raw_captures(
            None,
            organization_id=org,
            entity_type=None,
            capture_surface=None,
            review_state=None,
            project_ids=[project_id],
            limit=10,
            offset=0,
        )
        return [capture.id for capture in captures]

    assert await listed("project_1") == []

    await file_capture_sidecars_under_project(content_client.execute_query)

    expected = {
        private_sidecar.id: "project_1",
        project_sidecar.id: "project_2",
        plain_sidecar.id: None,
    }
    for sidecar in (private_sidecar, project_sidecar, plain_sidecar):
        stored = await _stored(org, sidecar)
        # Only the filing changes; the sidecar stays its writer's private row.
        assert (stored.memory_scope, stored.scope_key, stored.project_id) == (
            "private",
            None,
            expected[sidecar.id],
        )
    assert await listed("project_1") == [private_sidecar.id]
    assert await listed("project_2") == [project_sidecar.id]


@pytest.mark.asyncio
async def test_sidecar_backfill_touches_only_the_writers_unfiled_sidecar(
    content_client,
) -> None:
    org = uuid4()
    raw = _raw_memory(org, principal_id="owner", metadata={"project_id": "project_1"})
    foreign_sidecar = _sidecar(org, principal_id="someone-else")
    await _save(raw, foreign_sidecar)
    # A stamp bound by principal cannot name another writer's sidecar, so
    # forge the link directly to prove the backfill holds the line itself.
    await content_client.execute_query(
        "UPDATE raw_captures SET metadata.projected_capture_id = $sidecar WHERE uuid = $raw;",
        sidecar=str(foreign_sidecar.id),
        raw=str(raw.id),
    )
    other_org_raw = _raw_memory(uuid4(), metadata={"project_id": "project_1"})
    other_org_sidecar = _sidecar(org)
    filed_raw = _raw_memory(org, metadata={"project_id": "project_1"})
    filed_sidecar = _sidecar(org, project_id="project_9")
    await _save(other_org_raw, other_org_sidecar, filed_raw, filed_sidecar)
    assert await _stamp(org, filed_raw, filed_sidecar) is True
    await content_client.execute_query(
        "UPDATE raw_captures SET metadata.projected_capture_id = $sidecar WHERE uuid = $raw;",
        sidecar=str(other_org_sidecar.id),
        raw=str(other_org_raw.id),
    )

    await file_capture_sidecars_under_project(content_client.execute_query)
    await file_capture_sidecars_under_project(content_client.execute_query)

    for untouched in (foreign_sidecar, other_org_sidecar):
        stored = await _stored(org, untouched)
        assert stored.project_id is None
    stored = await _stored(org, filed_sidecar)
    assert stored.project_id == "project_9"
