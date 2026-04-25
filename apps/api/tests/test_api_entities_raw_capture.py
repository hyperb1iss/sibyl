from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.routes.entities import create_entity
from sibyl.api.schemas import EntityCreate
from sibyl.db.models import RawCapture
from sibyl_core.models.entities import EntityType


def _request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.cookies = {}
    return request


def _session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_quick_capture_creates_raw_archive_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.config import settings

    monkeypatch.setattr(settings, "store", "legacy")
    org = MagicMock()
    org.id = uuid4()

    ctx = MagicMock()
    ctx.user.id = uuid4()

    entity = EntityCreate(
        name="Quick memory",
        description="",
        content="remember this exact text",
        entity_type=EntityType.EPISODE,
        tags=["alpha", "beta"],
        metadata={
            "capture_mode": "quick",
            "capture_surface": "dashboard",
            "source": "notes",
        },
    )

    add_result = MagicMock()
    add_result.success = True
    add_result.id = "episode_new"
    add_result.message = "ok"

    content_session = _session()

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
    ):
        resp = await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=content_session,
            sync=False,
        )

    assert resp.id == "episode_new"
    content_session.add.assert_called_once()

    archive = content_session.add.call_args.args[0]
    assert isinstance(archive, RawCapture)
    assert archive.organization_id == org.id
    assert archive.entity_id == "episode_new"
    assert archive.title == "Quick memory"
    assert archive.raw_content == "remember this exact text"
    assert archive.entity_type == EntityType.EPISODE.value
    assert archive.tags == ["alpha", "beta"]
    assert archive.metadata_ == {
        "capture_mode": "quick",
        "capture_surface": "dashboard",
        "source": "notes",
    }
    assert archive.capture_surface == "dashboard"
    assert archive.created_by_user_id == ctx.user.id


@pytest.mark.asyncio
async def test_regular_entity_create_does_not_archive_raw_capture() -> None:
    org = MagicMock()
    org.id = uuid4()

    ctx = MagicMock()
    ctx.user.id = uuid4()

    entity = EntityCreate(
        name="Normal entity",
        description="",
        content="ordinary content",
        entity_type=EntityType.EPISODE,
        metadata={"source": "manual"},
    )

    add_result = MagicMock()
    add_result.success = True
    add_result.id = "episode_normal"
    add_result.message = "ok"

    content_session = _session()

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
    ):
        resp = await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=content_session,
            sync=False,
        )

    assert resp.id == "episode_normal"
    content_session.add.assert_not_called()
