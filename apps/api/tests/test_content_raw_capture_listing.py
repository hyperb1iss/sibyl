"""Raw capture listing hides a raw memory once its projection row exists."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from sibyl.persistence.content_common import RawCaptureRecord
from sibyl.persistence.surreal import content as surreal_content
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_RAW_CAPTURE_PROJECTION_FOLD_MIGRATION,
)


@pytest_asyncio.fixture
async def content_client(monkeypatch: pytest.MonkeyPatch):
    await surreal_content.close_shared_surreal_content_client()
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
async def test_projection_fold_migration_ignores_foreign_projections(content_client) -> None:
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

    await content_client.execute_query(CONTENT_RAW_CAPTURE_PROJECTION_FOLD_MIGRATION)

    stored = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=victim.id)
    assert stored is not None
    assert "projected_capture_id" not in stored.metadata
    listed, _ = await _list(org)
    assert victim.id in {capture.id for capture in listed}


@pytest.mark.asyncio
async def test_projection_fold_migration_stamps_legacy_pairs(content_client) -> None:
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

    await content_client.execute_query(CONTENT_RAW_CAPTURE_PROJECTION_FOLD_MIGRATION)

    after, _ = await _list(org)
    assert {capture.id for capture in after} == {projected.id, lone.id}
    stamped = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=raw.id)
    assert stamped is not None
    assert stamped.metadata["projected_capture_id"] == str(projected.id)
    untouched = await surreal_content.get_raw_capture(None, organization_id=org, capture_id=lone.id)
    assert untouched is not None
    assert "projected_capture_id" not in untouched.metadata
