from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.entities import (
    get_raw_capture,
    list_raw_captures,
    update_raw_capture_review_state,
)
from sibyl.api.schemas import RawCaptureReviewUpdate
from sibyl.db.models import RawCapture
from sibyl.persistence.content_common import RawCaptureRecord


def _org() -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    return org


def _capture(
    *,
    org_id,
    title: str,
    surface: str,
    entity_type: str = "episode",
    review_state: str | None = None,
) -> RawCapture:
    metadata = {"capture_mode": "quick", "capture_surface": surface}
    if review_state is not None:
        metadata["review_state"] = review_state

    return RawCapture(
        id=uuid4(),
        organization_id=org_id,
        entity_id="episode_123",
        title=title,
        raw_content=f"raw::{title}",
        entity_type=entity_type,
        tags=["alpha"],
        metadata_=metadata,
        capture_surface=surface,
        created_by_user_id=uuid4(),
        created_at=datetime(2026, 4, 14, 16, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_list_raw_captures_returns_paginated_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.config import settings

    monkeypatch.setattr(settings, "store", "legacy")
    org = _org()
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        _capture(org_id=org.id, title="Newest", surface="dashboard"),
        _capture(org_id=org.id, title="Older", surface="cli"),
        _capture(org_id=org.id, title="Overflow", surface="cli"),
    ]
    session.execute = AsyncMock(return_value=result)

    response = await list_raw_captures(
        org=org,
        session=session,
        entity_type=None,
        capture_surface=None,
        limit=2,
        offset=0,
    )

    assert response.limit == 2
    assert response.offset == 0
    assert response.has_more is True
    assert [capture.title for capture in response.captures] == ["Newest", "Older"]
    assert response.captures[0].metadata["capture_surface"] == "dashboard"
    assert response.captures[0].review_state == "pending"
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_raw_captures_supports_review_state_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.config import settings

    monkeypatch.setattr(settings, "store", "legacy")
    org = _org()
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        _capture(org_id=org.id, title="Deferred", surface="dashboard", review_state="deferred"),
    ]
    session.execute = AsyncMock(return_value=result)

    response = await list_raw_captures(
        org=org,
        session=session,
        entity_type=None,
        capture_surface=None,
        review_state="deferred",
        limit=10,
        offset=0,
    )

    assert [capture.review_state for capture in response.captures] == ["deferred"]
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_raw_capture_returns_verbatim_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.config import settings

    monkeypatch.setattr(settings, "store", "legacy")
    org = _org()
    capture = _capture(org_id=org.id, title="Quick memory", surface="dashboard")
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = capture
    session.execute = AsyncMock(return_value=result)

    response = await get_raw_capture(capture.id, org=org, session=session)

    assert response.id == str(capture.id)
    assert response.title == "Quick memory"
    assert response.raw_content == "raw::Quick memory"
    assert response.capture_surface == "dashboard"
    assert response.review_state == "pending"


@pytest.mark.asyncio
async def test_get_raw_capture_raises_not_found_for_other_org() -> None:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    with pytest.raises(HTTPException) as exc:
        await get_raw_capture(uuid4(), org=_org(), session=session)

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
            "sibyl.api.routes.entities.save_raw_capture_record",
            AsyncMock(side_effect=lambda _session, *, capture: capture),
        ) as save_capture,
    ):
        response = await update_raw_capture_review_state(
            capture.id,
            RawCaptureReviewUpdate(review_state="deferred"),
            org=org,
            session=session,
        )

    assert response.review_state == "deferred"
    saved = save_capture.await_args.kwargs["capture"]
    assert saved.metadata["review_state"] == "deferred"
    assert "reviewed_at" in saved.metadata
    assert "deferred_at" in saved.metadata
    save_capture.assert_awaited_once()
