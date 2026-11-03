from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.entity_captures import (
    archive_raw_capture,
    get_raw_capture,
    list_raw_captures,
    update_raw_capture_review_state,
)
from sibyl.api.schemas import RawCaptureReviewUpdate
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl_core.auth import ProjectRole
from sibyl_core.auth.models import OrganizationRole
from tests.harness.auth import stub_auth_context


def _org() -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    return org


def _ctx(*, user_id: str) -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.org_role = OrganizationRole.MEMBER
    ctx.api_key_memory_scope_keys = None
    return ctx


def _capture(
    *,
    org_id,
    title: str,
    surface: str,
    entity_type: str = "episode",
    review_state: str | None = None,
    owner_id: UUID | None = None,
) -> RawCaptureRecord:
    owner_id = owner_id or uuid4()
    metadata = {
        "capture_mode": "quick",
        "capture_surface": surface,
        "memory_scope": "private",
        "principal_id": str(owner_id),
    }
    if review_state is not None:
        metadata["review_state"] = review_state

    return RawCaptureRecord(
        id=uuid4(),
        organization_id=org_id,
        entity_id="episode_123",
        title=title,
        raw_content=f"raw::{title}",
        entity_type=entity_type,
        tags=["alpha"],
        metadata=metadata,
        capture_surface=surface,
        review_state=review_state or "pending",
        created_by_user_id=owner_id,
        created_at=datetime(2026, 4, 14, 16, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_list_raw_captures_returns_paginated_summaries() -> None:
    org = _org()
    session = MagicMock()
    reader_id = uuid4()
    captures = [
        _capture(org_id=org.id, title="Newest", surface="dashboard", owner_id=reader_id),
        _capture(org_id=org.id, title="Older", surface="cli", owner_id=reader_id),
    ]

    with patch(
        "sibyl.api.routes.entity_captures.content_runtime.list_raw_captures",
        AsyncMock(return_value=(captures, True)),
    ) as list_captures:
        response = await list_raw_captures(
            org=org,
            ctx=_ctx(user_id=str(reader_id)),
            session=session,
            entity_type=None,
            capture_surface=None,
            review_state=None,
            limit=2,
            offset=0,
        )

    assert response.limit == 2
    assert response.offset == 0
    assert response.has_more is True
    assert [capture.title for capture in response.captures] == ["Newest", "Older"]
    assert response.captures[0].metadata["capture_surface"] == "dashboard"
    assert response.captures[0].review_state == "pending"
    assert response.captures[0].memory_scope == "private"
    assert response.captures[0].project_id is None
    list_captures.assert_awaited_once_with(
        session,
        organization_id=org.id,
        entity_type=None,
        capture_surface=None,
        review_state=None,
        limit=2,
        offset=0,
        private_owner=str(reader_id),
    )


@pytest.mark.asyncio
async def test_list_raw_captures_supports_review_state_filter() -> None:
    org = _org()
    session = MagicMock()
    captures = [
        _capture(org_id=org.id, title="Deferred", surface="dashboard", review_state="deferred"),
    ]

    with patch(
        "sibyl.api.routes.entity_captures.content_runtime.list_raw_captures",
        AsyncMock(return_value=(captures, False)),
    ) as list_captures:
        response = await list_raw_captures(
            org=org,
            ctx=_ctx(user_id=str(captures[0].created_by_user_id)),
            session=session,
            entity_type=None,
            capture_surface=None,
            review_state="deferred",
            limit=10,
            offset=0,
        )

    assert [capture.review_state for capture in response.captures] == ["deferred"]
    assert list_captures.await_args.kwargs["review_state"] == "deferred"


@pytest.mark.asyncio
async def test_get_raw_capture_returns_verbatim_content() -> None:
    org = _org()
    capture = _capture(org_id=org.id, title="Quick memory", surface="dashboard")
    session = MagicMock()

    with patch(
        "sibyl.api.routes.entity_captures.content_runtime.get_raw_capture",
        AsyncMock(return_value=capture),
    ) as load_capture:
        response = await get_raw_capture(
            capture.id,
            org=org,
            ctx=_ctx(user_id=str(capture.created_by_user_id)),
            session=session,
        )

    assert response.id == str(capture.id)
    assert response.title == "Quick memory"
    assert response.raw_content == "raw::Quick memory"
    assert response.capture_surface == "dashboard"
    assert response.review_state == "pending"
    load_capture.assert_awaited_once_with(
        session,
        organization_id=org.id,
        capture_id=capture.id,
    )


@pytest.mark.asyncio
async def test_get_raw_capture_raises_not_found_for_other_org() -> None:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    with pytest.raises(HTTPException) as exc:
        await get_raw_capture(
            uuid4(),
            org=_org(),
            ctx=_ctx(user_id=str(uuid4())),
            session=session,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_raw_capture_hides_other_users_private_capture() -> None:
    org = _org()
    capture = _capture(org_id=org.id, title="Someone else's note", surface="dashboard")
    session = MagicMock()

    with (
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.get_raw_capture",
            AsyncMock(return_value=capture),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await get_raw_capture(
            capture.id,
            org=org,
            ctx=_ctx(user_id=str(uuid4())),
            session=session,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_raw_capture_review_state_updates_metadata() -> None:
    org = _org()
    capture = RawCaptureRecord(
        id=uuid4(),
        organization_id=org.id,
        entity_id="episode_123",
        title="Quick memory",
        raw_content="raw::Quick memory",
        entity_type="episode",
        tags=["alpha"],
        metadata={"capture_mode": "quick", "capture_surface": "dashboard"},
        capture_surface="dashboard",
        created_by_user_id=uuid4(),
        created_at=datetime(2026, 4, 14, 16, 0, tzinfo=UTC),
    )
    session = MagicMock()

    with (
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.get_raw_capture",
            AsyncMock(return_value=capture),
        ),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.update_raw_capture_review_state",
            AsyncMock(
                side_effect=lambda _session, **_kwargs: replace(
                    capture,
                    review_state="deferred",
                    metadata={
                        **capture.metadata,
                        "review_state": "deferred",
                        "reviewed_at": "2026-04-14T16:01:00Z",
                        "deferred_at": "2026-04-14T16:01:00Z",
                    },
                )
            ),
        ) as update_capture,
    ):
        response = await update_raw_capture_review_state(
            capture.id,
            RawCaptureReviewUpdate(review_state="deferred"),
            org=org,
            ctx=_ctx(user_id=str(capture.created_by_user_id)),
            session=session,
        )

    assert response.review_state == "deferred"
    update_capture.assert_awaited_once_with(
        session,
        organization_id=org.id,
        capture_id=capture.id,
        review_state="deferred",
    )


@pytest.mark.asyncio
async def test_update_raw_capture_review_state_hidden_for_other_user() -> None:
    org = _org()
    capture = _capture(org_id=org.id, title="Someone else's note", surface="dashboard")
    session = MagicMock()

    with (
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.get_raw_capture",
            AsyncMock(return_value=capture),
        ),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.update_raw_capture_review_state",
            AsyncMock(),
        ) as update_capture,
        pytest.raises(HTTPException) as exc,
    ):
        await update_raw_capture_review_state(
            capture.id,
            RawCaptureReviewUpdate(review_state="deferred"),
            org=org,
            ctx=_ctx(user_id=str(uuid4())),
            session=session,
        )

    assert exc.value.status_code == 404
    update_capture.assert_not_awaited()


_PROJECT = "project-a"


def _project_capture(*, org_id: UUID, author_id: UUID) -> RawCaptureRecord:
    return RawCaptureRecord(
        id=uuid4(),
        organization_id=org_id,
        entity_id="reflection_123",
        title="Reflection candidate",
        raw_content="raw::Reflection candidate",
        entity_type="episode",
        principal_id=str(author_id),
        memory_scope="project",
        scope_key=_PROJECT,
        project_id=_PROJECT,
        review_state="promoted",
        metadata={"capture_surface": "reflection_candidate", "review_state": "promoted"},
        capture_surface="reflection_candidate",
        created_by_user_id=author_id,
        created_at=datetime(2026, 4, 14, 16, 0, tzinfo=UTC),
    )


def _project_grants(
    *,
    read: set[str],
    write: set[str] = frozenset(),
    maintain: set[str] = frozenset(),
):
    grants = {
        ProjectRole.VIEWER: read,
        ProjectRole.CONTRIBUTOR: write,
        ProjectRole.MAINTAINER: maintain,
    }
    return patch(
        "sibyl.api.routes.entity_policy.list_accessible_project_graph_ids",
        AsyncMock(
            side_effect=lambda _ctx, required_role=ProjectRole.VIEWER: set(grants[required_role])
        ),
    )


async def _patch_review_state(capture: RawCaptureRecord, *, reader_id: UUID, ctx=None):
    update = AsyncMock(
        side_effect=lambda _session, **kwargs: replace(capture, review_state=kwargs["review_state"])
    )
    with (
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.get_raw_capture",
            AsyncMock(return_value=capture),
        ),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.update_raw_capture_review_state",
            update,
        ),
    ):
        try:
            response = await update_raw_capture_review_state(
                capture.id,
                RawCaptureReviewUpdate(review_state="pending"),
                org=MagicMock(id=capture.organization_id),
                ctx=ctx or _ctx(user_id=str(reader_id)),
                session=MagicMock(),
            )
        except HTTPException as exc:
            return exc, update
    return response, update


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grants", "reason"),
    [
        pytest.param({"read": {_PROJECT}}, "unverified_membership", id="viewer"),
        pytest.param(
            {"read": {_PROJECT}, "write": {_PROJECT}},
            "project_maintainer_required",
            id="contributor",
        ),
        pytest.param(
            {"read": {_PROJECT}, "write": {_PROJECT}, "maintain": {"project-b"}},
            "project_maintainer_required",
            id="maintainer-elsewhere",
        ),
    ],
)
async def test_review_state_of_another_principals_project_capture_needs_maintainer(
    grants, reason
) -> None:
    capture = _project_capture(org_id=uuid4(), author_id=uuid4())

    with _project_grants(**grants):
        result, update = await _patch_review_state(capture, reader_id=uuid4())

    assert isinstance(result, HTTPException)
    assert result.status_code == 403
    assert result.detail == reason
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintainer_can_set_review_state_on_another_principals_project_capture() -> None:
    capture = _project_capture(org_id=uuid4(), author_id=uuid4())

    with _project_grants(read={_PROJECT}, write={_PROJECT}, maintain={_PROJECT}):
        result, update = await _patch_review_state(capture, reader_id=uuid4())

    assert not isinstance(result, HTTPException), result
    assert result.review_state == "pending"
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_author_contributor_can_set_review_state_on_their_project_capture() -> None:
    author_id = uuid4()
    capture = _project_capture(org_id=uuid4(), author_id=author_id)

    with _project_grants(read={_PROJECT}, write={_PROJECT}):
        result, update = await _patch_review_state(capture, reader_id=author_id)

    assert not isinstance(result, HTTPException), result
    assert result.review_state == "pending"
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_author_needs_project_write_access_to_set_review_state() -> None:
    author_id = uuid4()
    capture = _project_capture(org_id=uuid4(), author_id=author_id)

    with _project_grants(read={_PROJECT}):
        result, update = await _patch_review_state(capture, reader_id=author_id)

    assert isinstance(result, HTTPException)
    assert result.status_code == 403
    assert result.detail == "unverified_membership"
    update.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("org_role", "allowed"),
    [(OrganizationRole.MEMBER, False), (OrganizationRole.ADMIN, True)],
)
async def test_org_admins_hold_maintainer_on_every_project_for_review_state(
    org_role, allowed
) -> None:
    from sibyl.persistence.surreal.auth_runtime import projects

    capture = _project_capture(org_id=uuid4(), author_id=uuid4())
    reader_id = uuid4()
    ctx = SimpleNamespace(
        user=SimpleNamespace(id=reader_id),
        user_id=str(reader_id),
        organization=object(),
        org_role=org_role,
        api_key_project_ids=None,
        api_key_memory_scope_keys=None,
        accessible_teams=frozenset(),
        accessible_delegations=frozenset(),
    )
    record = {
        "uuid": "project-uuid",
        "graph_project_id": _PROJECT,
        "visibility": "org",
        "default_role": ProjectRole.CONTRIBUTOR.value,
    }
    with patch.object(
        projects, "_load_project_access_records", AsyncMock(return_value=([record], {}))
    ):
        result, update = await _patch_review_state(capture, reader_id=reader_id, ctx=ctx)

    if allowed:
        assert not isinstance(result, HTTPException), result
        update.assert_awaited_once()
    else:
        assert isinstance(result, HTTPException)
        assert result.detail == "project_maintainer_required"
        update.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_raw_capture_files_the_sidecar_under_the_claimed_id() -> None:
    """The claim already pointed the raw memory at this id; the sidecar must take it."""
    org_id = uuid4()
    user_id = uuid4()
    claimed_id = uuid4()
    saved: list[RawCaptureRecord] = []

    async def fake_save(_session, *, capture: RawCaptureRecord) -> RawCaptureRecord:
        saved.append(capture)
        return capture

    mark = AsyncMock()
    with (
        patch("sibyl.api.routes.entity_captures.save_raw_capture_record", fake_save),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.mark_raw_capture_projected",
            mark,
        ),
    ):
        await archive_raw_capture(
            None,
            organization_id=org_id,
            user_id=user_id,
            entity_id="error_pattern_abc123",
            entity_name="zsh pipeline trap",
            entity_content="paste mid-pipeline swallows stray stdin",
            entity_type="error_pattern",
            tags=[],
            metadata={"capture_mode": "remember"},
            project_id=None,
            capture_id=claimed_id,
        )

    assert [capture.id for capture in saved] == [claimed_id]
    assert "raw_memory_id" not in saved[0].metadata
    # Stamping happens in the claim, before the graph write; archiving the
    # sidecar never stamps a raw row on its own.
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_raw_capture_leaves_unprojected_quick_captures_alone() -> None:
    mark = AsyncMock(return_value=None)
    save = AsyncMock(side_effect=lambda _session, *, capture: capture)
    with (
        patch("sibyl.api.routes.entity_captures.save_raw_capture_record", save),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.mark_raw_capture_projected",
            mark,
        ),
    ):
        await archive_raw_capture(
            None,
            organization_id=uuid4(),
            user_id=None,
            entity_id="note_1",
            entity_name="quick note",
            entity_content="body",
            entity_type="note",
            tags=[],
            metadata={"capture_mode": "quick"},
            project_id=None,
        )

    save.assert_awaited_once()
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_project_selection_narrows_private_rows_without_granting_ownership():
    org = _org()
    reader = uuid4()
    selected = _capture(org_id=org.id, title="Selected", surface="cli", owner_id=reader)
    selected = replace(selected, project_id="a")
    foreign_project = replace(selected, id=uuid4(), title="Foreign project", project_id="b")
    foreign_owner = _capture(org_id=org.id, title="Foreign owner", surface="cli")
    foreign_owner = replace(foreign_owner, project_id="a")
    with (
        patch(
            "sibyl.api.routes.entity_captures.policy.verify_entity_project_access", AsyncMock()
        ) as verify,
        patch(
            "sibyl.api.routes.entity_captures.policy.accessible_project_ids_for_read",
            AsyncMock(return_value={"a", "b"}),
        ),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.list_raw_captures",
            AsyncMock(return_value=([selected, foreign_project, foreign_owner], False)),
        ) as storage,
    ):
        result = await list_raw_captures(
            org=org,
            ctx=_ctx(user_id=str(reader)),
            session=MagicMock(),
            entity_type=None,
            capture_surface=None,
            review_state=None,
            project_ids=["a"],
            limit=10,
            offset=0,
        )
    assert [capture.title for capture in result.captures] == ["Selected"]
    assert storage.await_args.kwargs["project_ids"] == ["a"]
    assert verify.await_args.args[2] == "a"
    assert verify.await_args.kwargs["require_existing_project"] is True
    assert result.scope == "project_selection"
    assert result.project_ids == ["a"]


@pytest.mark.asyncio
async def test_capture_project_selection_checks_access_before_storage():
    with (
        patch(
            "sibyl.api.routes.entity_captures.policy.verify_entity_project_access",
            AsyncMock(side_effect=HTTPException(403, "denied")),
        ),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.list_raw_captures", AsyncMock()
        ) as storage,
        pytest.raises(HTTPException),
    ):
        await list_raw_captures(
            org=_org(),
            ctx=_ctx(user_id=str(uuid4())),
            session=MagicMock(),
            entity_type=None,
            capture_surface=None,
            review_state=None,
            project_ids=["foreign"],
            limit=10,
            offset=0,
        )
    storage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [["b"], ["a", "b"]])
@pytest.mark.parametrize("grants", [{"a"}, set()])
async def test_capture_selection_cannot_exceed_owner_api_key_project_grants(selection, grants):
    ctx = stub_auth_context(org_role=OrganizationRole.OWNER, api_key_project_ids=frozenset(grants))
    with (
        patch(
            "sibyl.api.routes.entity_captures.policy.verify_entity_project_access", AsyncMock()
        ) as verify,
        patch(
            "sibyl.api.routes.entity_captures.policy.accessible_project_ids_for_read",
            AsyncMock(return_value=grants),
        ),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.list_raw_captures", AsyncMock()
        ) as storage,
        pytest.raises(HTTPException) as denied,
    ):
        await list_raw_captures(
            org=_org(),
            ctx=ctx,
            session=MagicMock(),
            entity_type=None,
            capture_surface=None,
            review_state=None,
            project_ids=selection,
            limit=10,
            offset=0,
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "project_scope_denied"
    verify.assert_not_awaited()
    storage.assert_not_awaited()


def test_capture_summary_reports_scope_and_project() -> None:
    from sibyl.api.routes.entity_serialization import serialize_raw_capture_summary

    org_id = uuid4()
    sidecar = replace(
        _capture(org_id=org_id, title="Sidecar", surface="cli"),
        memory_scope="project",
        scope_key="project_1",
        project_id="project_1",
    )
    raw = _capture(org_id=org_id, title="Raw", surface="cli")
    raw = replace(raw, metadata={**raw.metadata, "project_id": "project_2"})

    assert serialize_raw_capture_summary(sidecar).memory_scope == "project"
    assert serialize_raw_capture_summary(sidecar).project_id == "project_1"
    # Raw memories written by the CLI carry their project in metadata only.
    assert serialize_raw_capture_summary(raw).project_id == "project_2"
