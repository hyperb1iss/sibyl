from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.idempotency import idempotency_request_hash
from sibyl.api.routes.entities import create_entity
from sibyl.api.schemas import EntityCreate, EntityResponse
from sibyl.persistence.content_common import ApiIdempotencyRecord, RawCaptureRecord
from sibyl_core.models.entities import EntityType


def _request(*, idempotency_key: str | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    if idempotency_key:
        request.headers["Idempotency-Key"] = idempotency_key
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
    save_capture = AsyncMock(side_effect=lambda _session, *, capture: capture)

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.save_raw_capture_record", save_capture),
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
    content_session.add.assert_not_called()
    save_capture.assert_awaited_once()

    archive = save_capture.await_args.kwargs["capture"]
    assert isinstance(archive, RawCaptureRecord)
    assert archive.organization_id == org.id
    assert archive.entity_id == "episode_new"
    assert archive.title == "Quick memory"
    assert archive.raw_content == "remember this exact text"
    assert archive.entity_type == EntityType.EPISODE.value
    assert archive.principal_id == str(ctx.user.id)
    assert archive.memory_scope == "private"
    assert archive.review_state == "pending"
    assert archive.tags == ["alpha", "beta"]
    assert archive.metadata == {
        "capture_mode": "quick",
        "capture_surface": "dashboard",
        "source": "notes",
    }
    assert archive.capture_surface == "dashboard"
    assert archive.created_by_user_id == ctx.user.id


@pytest.mark.asyncio
async def test_create_entity_replays_saved_idempotent_response() -> None:
    org = MagicMock()
    org.id = uuid4()

    ctx = MagicMock()
    ctx.user.id = uuid4()

    entity = EntityCreate(
        name="Replay memory",
        content="do not duplicate this capture",
        entity_type=EntityType.EPISODE,
        metadata={"capture_mode": "remember"},
    )
    response = EntityResponse(
        id="episode_saved",
        entity_type=EntityType.EPISODE,
        name="Replay memory",
        description="",
        content="do not duplicate this capture",
        category=None,
        languages=[],
        tags=[],
        metadata={"organization_id": str(org.id), "capture_mode": "remember"},
        source_file=None,
        created_at=None,
        updated_at=None,
    )
    payload = {"body": entity.model_dump(mode="json"), "query": {"sync": False}}
    record = ApiIdempotencyRecord(
        organization_id=org.id,
        principal_id=str(ctx.user.id),
        idempotency_key="idem-entity",
        method="POST",
        path="/entities",
        request_hash=idempotency_request_hash(payload),
        response_status_code=201,
        response_body=response.model_dump(mode="json"),
    )
    add = AsyncMock()

    with (
        patch("sibyl_core.tools.core.add", add),
        patch(
            "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
            AsyncMock(return_value=record),
        ),
    ):
        replayed = await create_entity(
            request=_request(idempotency_key="idem-entity"),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=_session(),
            sync=False,
        )

    assert replayed.id == "episode_saved"
    add.assert_not_awaited()


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
    save_capture = AsyncMock()

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.save_raw_capture_record", save_capture),
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
    save_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_capture_creates_raw_archive_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from sibyl.config import settings

    monkeypatch.setattr(settings, "store", "legacy")
    org = MagicMock()
    org.id = uuid4()

    ctx = MagicMock()
    ctx.user.id = uuid4()

    entity = EntityCreate(
        name="Architecture decision",
        description="",
        content="Use first-class context packs for agent injection.",
        entity_type=EntityType.DECISION,
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "decision",
        },
    )

    add_result = MagicMock()
    add_result.success = True
    add_result.id = "decision_new"
    add_result.message = "ok"

    content_session = _session()
    save_capture = AsyncMock(side_effect=lambda _session, *, capture: capture)

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch("sibyl.api.routes.entities.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entities.save_raw_capture_record", save_capture),
    ):
        resp = await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=content_session,
            sync=False,
        )

    assert resp.id == "decision_new"
    archive = save_capture.await_args.kwargs["capture"]
    assert archive.entity_id == "decision_new"
    assert archive.raw_content == "Use first-class context packs for agent injection."
    assert archive.entity_type == EntityType.DECISION.value
    assert archive.capture_surface == "cli"
