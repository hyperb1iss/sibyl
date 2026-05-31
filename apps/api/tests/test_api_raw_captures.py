from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.entities import (
    get_raw_capture,
    list_raw_captures,
    update_raw_capture_review_state,
)
from sibyl.api.schemas import RawCaptureReviewUpdate
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl_core.auth.models import OrganizationRole


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
        "sibyl.api.routes.entities.content_runtime.list_raw_captures",
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
    list_captures.assert_awaited_once_with(
        session,
        organization_id=org.id,
        entity_type=None,
        capture_surface=None,
        review_state=None,
        limit=2,
        offset=0,
    )


@pytest.mark.asyncio
async def test_list_raw_captures_supports_review_state_filter() -> None:
    org = _org()
    session = MagicMock()
    captures = [
        _capture(org_id=org.id, title="Deferred", surface="dashboard", review_state="deferred"),
    ]

    with patch(
        "sibyl.api.routes.entities.content_runtime.list_raw_captures",
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
        "sibyl.api.routes.entities.content_runtime.get_raw_capture",
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
            "sibyl.api.routes.entities.content_runtime.get_raw_capture",
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
            "sibyl.api.routes.entities.content_runtime.get_raw_capture",
            AsyncMock(return_value=capture),
        ),
        patch(
            "sibyl.api.routes.entities.content_runtime.update_raw_capture_review_state",
            AsyncMock(
                side_effect=lambda _session, **_kwargs: replace(
                    capture,
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
            "sibyl.api.routes.entities.content_runtime.get_raw_capture",
            AsyncMock(return_value=capture),
        ),
        patch(
            "sibyl.api.routes.entities.content_runtime.update_raw_capture_review_state",
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
