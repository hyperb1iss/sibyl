from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from structlog.testing import capture_logs

from sibyl.api.idempotency import idempotency_request_hash
from sibyl.api.routes.entity_mutations import create_entity
from sibyl.api.routes.entity_policy import raw_capture_visible_to_reader
from sibyl.api.schemas import EntityCreate, EntityResponse
from sibyl.persistence.content_common import ApiIdempotencyRecord, RawCaptureRecord
from sibyl_core.models.entities import EntityType
from tests.harness.auth import stub_auth_context


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


def _claimed_row(raw_id: str, *, revision: int = 1, **metadata: object) -> dict[str, object]:
    """What the claim statement returns for the raw row it stamped."""
    return {
        "uuid": raw_id,
        "source_id": "cli:manual",
        "revision": revision,
        "metadata": dict(metadata),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source_capture_id", [None, "raw-1"])
async def test_quick_capture_creates_raw_archive_record(
    monkeypatch: pytest.MonkeyPatch,
    source_capture_id: str | None,
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
            "memory_scope": "private",
            "raw_memory_id": source_capture_id,
            "correction_history": [],
            "source_bindings": {"planted": 9},
        },
    )

    add_result = MagicMock()
    add_result.success = True
    add_result.id = "episode_new"
    add_result.message = "ok"

    content_session = _session()
    save_capture = AsyncMock(side_effect=lambda _session, *, capture: capture)
    claimed = _claimed_row("raw-1", revision=2, source_bindings={"upstream": 1})
    mark_projected = AsyncMock(return_value=claimed)
    add = AsyncMock(return_value=add_result)

    with (
        patch("sibyl_core.tools.core.add", add),
        patch("sibyl.api.routes.entity_mutations.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entity_captures.save_raw_capture_record", save_capture),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.mark_raw_capture_projected",
            mark_projected,
        ),
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
    provenance = add.await_args.kwargs["capture_provenance"]
    if source_capture_id is None:
        mark_projected.assert_not_awaited()
        assert provenance is None
        assert "raw_memory_id" not in resp.metadata
    else:
        # The claim runs before the graph write and names the sidecar the
        # route then archives, so the fold and the bind are the same proof.
        mark_projected.assert_awaited_once_with(
            content_session,
            organization_id=org.id,
            raw_capture_id=source_capture_id,
            projected_capture_id=archive.id,
            principal_id=str(ctx.user.id),
            memory_scope="private",
            scope_key=None,
        )
        # Built from the stored raw row: the planted bindings never survive,
        # and the row inherits the capture's own upstream binding as MCP does.
        assert provenance == {
            "raw_memory_id": "raw-1",
            "raw_source_id": "cli:manual",
            "source_bindings": {"raw-1": 2, "upstream": 1},
        }
        assert resp.metadata["raw_memory_id"] == "raw-1"
    assert add.await_args.kwargs["metadata"].get("source_bindings") == (
        None if provenance is None else provenance["source_bindings"]
    )


def _remember_entity(raw_memory_id: str, **metadata: object) -> EntityCreate:
    return EntityCreate(
        name="CLI memory",
        content="the exact text the CLI stored raw",
        entity_type=EntityType.EPISODE,
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "episode",
            "memory_scope": "private",
            "raw_memory_id": raw_memory_id,
            **metadata,
        },
    )


async def _create_remembered(
    entity: EntityCreate,
    *,
    claimed: dict[str, object] | None,
    add_result: object,
    release: AsyncMock | None = None,
) -> tuple[object, AsyncMock, AsyncMock, AsyncMock, AsyncMock, list[dict[str, object]]]:
    org = SimpleNamespace(id=uuid4())
    ctx = stub_auth_context(user_id=uuid4(), organization_id=org.id)
    add = AsyncMock(return_value=add_result)
    mark = AsyncMock(return_value=claimed)
    release = release or AsyncMock(return_value=True)
    save_capture = AsyncMock(side_effect=lambda _session, *, capture: capture)

    with (
        capture_logs() as logs,
        patch("sibyl_core.tools.core.add", add),
        patch("sibyl.api.routes.entity_mutations.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entity_captures.save_raw_capture_record", save_capture),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.mark_raw_capture_projected",
            mark,
        ),
        patch(
            "sibyl.api.routes.entity_captures.content_runtime.release_raw_capture_projection",
            release,
        ),
    ):
        try:
            outcome: object = await create_entity(
                request=_request(),
                entity=entity,
                org=org,
                ctx=ctx,
                content_session=None,
                sync=False,
            )
        except Exception as exc:
            outcome = exc
    return outcome, add, mark, release, save_capture, logs


def _refusals(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == "entity_raw_capture_binding_refused"]


@pytest.mark.asyncio
async def test_an_unclaimable_raw_memory_id_writes_the_row_unbound() -> None:
    """A forged or foreign id fails the claim, and the row still lands with no link."""
    outcome, add, mark, release, save_capture, logs = await _create_remembered(
        _remember_entity("someone-elses-capture"),
        claimed=None,
        add_result=SimpleNamespace(success=True, id="episode_new", message="ok"),
    )

    assert isinstance(outcome, EntityResponse)
    mark.assert_awaited_once()
    assert add.await_args.kwargs["capture_provenance"] is None
    assert "raw_memory_id" not in add.await_args.kwargs["metadata"]
    assert "raw_memory_id" not in outcome.metadata
    release.assert_not_awaited()
    # The sidecar is not the id the failed claim proposed, so nothing points
    # the foreign raw row at it.
    archived = save_capture.await_args.kwargs["capture"]
    assert archived.id != mark.await_args.kwargs["projected_capture_id"]
    assert [(entry["raw_memory_id"], entry["reason"]) for entry in _refusals(logs)] == [
        ("someone-elses-capture", "capture_not_claimable")
    ]


@pytest.mark.asyncio
async def test_a_failed_graph_write_gives_the_claimed_capture_back() -> None:
    outcome, add, mark, release, save_capture, _logs = await _create_remembered(
        _remember_entity("raw-1"),
        claimed=_claimed_row("raw-1"),
        add_result=SimpleNamespace(success=False, id=None, message="writer refused"),
    )

    assert isinstance(outcome, HTTPException)
    add.assert_awaited_once()
    save_capture.assert_not_awaited()
    release.assert_awaited_once()
    assert release.await_args.kwargs["raw_capture_id"] == "raw-1"
    assert (
        release.await_args.kwargs["projected_capture_id"]
        == mark.await_args.kwargs["projected_capture_id"]
    )


@pytest.mark.asyncio
async def test_a_claimed_row_without_a_revision_is_released_and_left_unbound() -> None:
    claimed = _claimed_row("raw-1")
    claimed["revision"] = None
    outcome, add, _mark, release, _save, logs = await _create_remembered(
        _remember_entity("raw-1"),
        claimed=claimed,
        add_result=SimpleNamespace(success=True, id="episode_new", message="ok"),
    )

    assert isinstance(outcome, EntityResponse)
    assert add.await_args.kwargs["capture_provenance"] is None
    release.assert_awaited_once()
    assert [entry["reason"] for entry in _refusals(logs)] == ["capture_bindings_malformed"]


@pytest.mark.asyncio
async def test_a_raw_memory_id_on_a_plain_write_is_never_claimed() -> None:
    """Only a capture write archives the sidecar a claim has to point at."""
    entity = _remember_entity("raw-1")
    entity.metadata.pop("capture_mode")
    outcome, add, mark, _release, save_capture, logs = await _create_remembered(
        entity,
        claimed=_claimed_row("raw-1"),
        add_result=SimpleNamespace(success=True, id="episode_new", message="ok"),
    )

    assert isinstance(outcome, EntityResponse)
    mark.assert_not_awaited()
    save_capture.assert_not_awaited()
    assert add.await_args.kwargs["capture_provenance"] is None
    assert [entry["reason"] for entry in _refusals(logs)] == ["not_a_capture_write"]


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
            "sibyl.api.idempotency.content_runtime.reserve_api_idempotency_record",
            AsyncMock(return_value=(record, False)),
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
        patch("sibyl.api.routes.entity_mutations.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entity_captures.save_raw_capture_record", save_capture),
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
        patch("sibyl.api.routes.entity_mutations.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entity_captures.save_raw_capture_record", save_capture),
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


_TEAM = "0f8b6c52-4d2e-4a7a-9c1d-6e5b2a9f3d10"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_scope", "scope_key"),
    [
        ("private", "project_forged"),
        ("project", "project_forged"),
        # A team the writer belongs to: the graph row is keyed to it, the sidecar is not.
        ("team", _TEAM),
    ],
)
async def test_remember_sidecar_is_filed_under_the_verified_project(
    monkeypatch: pytest.MonkeyPatch,
    memory_scope: str,
    scope_key: str,
) -> None:
    """The sidecar lands in its project and stays private to its writer.

    It has no lifecycle of its own, so whatever audience the memory declares,
    widening the sidecar would serve it past corrections to the memory.
    """
    from sibyl.config import settings

    monkeypatch.setattr(settings, "store", "legacy")
    org = MagicMock()
    org.id = uuid4()

    ctx = stub_auth_context(user_id=uuid4())

    entity = EntityCreate(
        name="Scoped decision",
        description="",
        content="Captures carry the project they were written in.",
        entity_type=EntityType.DECISION,
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "decision",
            "project_id": "project_abc",
            "memory_scope": memory_scope,
            # Forged owner fields never reach the sidecar.
            "scope_key": scope_key,
            "principal_id": "someone-else",
        },
    )

    add_result = MagicMock()
    add_result.success = True
    add_result.id = "decision_scoped"
    add_result.message = "ok"

    save_capture = AsyncMock(side_effect=lambda _session, *, capture: capture)
    verify_project = AsyncMock()

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        patch("sibyl.api.routes.entity_mutations.broadcast_event", AsyncMock()),
        patch("sibyl.api.routes.entity_mutations.verify_entity_project_access", verify_project),
        patch("sibyl.api.routes.entity_captures.save_raw_capture_record", save_capture),
        patch(
            "sibyl.api.routes.memory_auth.list_accessible_team_scope_keys",
            AsyncMock(return_value={_TEAM}),
        ),
    ):
        await create_entity(
            request=_request(),
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=_session(),
            sync=False,
        )

    verify_project.assert_awaited_once()
    archive = save_capture.await_args.kwargs["capture"]
    assert archive.project_id == "project_abc"
    assert archive.memory_scope == "private"
    assert archive.scope_key is None
    assert archive.principal_id == str(ctx.user.id)
    for owner_key in ("project_id", "memory_scope", "scope_key", "principal_id"):
        assert owner_key not in archive.metadata


def _reader_ctx(
    *,
    user_id: str = "reader-1",
    api_key_memory_scope_keys: set[str] | None = None,
    accessible_delegations: frozenset[str] = frozenset(),
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        organization_id="org-1",
        org_role=None,
        api_key_memory_scope_keys=api_key_memory_scope_keys,
        accessible_teams=frozenset(),
        accessible_delegations=accessible_delegations,
    )


def test_raw_capture_visibility_denies_delegated_without_membership() -> None:
    capture = RawCaptureRecord(
        organization_id=uuid4(),
        title="Delegated secret",
        raw_content="sensitive delegated raw text",
        entity_type=EntityType.EPISODE.value,
        principal_id="owner-1",
        memory_scope="delegated",
        scope_key="agent:secret",
    )

    assert (
        raw_capture_visible_to_reader(
            capture,
            ctx=_reader_ctx(),
            accessible_projects=set(),
        )
        is False
    )


def test_raw_capture_visibility_allows_delegated_membership() -> None:
    capture = RawCaptureRecord(
        organization_id=uuid4(),
        title="Delegated note",
        raw_content="delegated raw text",
        entity_type=EntityType.EPISODE.value,
        principal_id="owner-1",
        memory_scope="delegated",
        scope_key="agent:nova",
    )

    assert (
        raw_capture_visible_to_reader(
            capture,
            ctx=_reader_ctx(accessible_delegations=frozenset({"agent:nova"})),
            accessible_projects=set(),
        )
        is True
    )


def test_raw_capture_visibility_denies_unknown_scope() -> None:
    capture = RawCaptureRecord(
        organization_id=uuid4(),
        title="Unsupported",
        raw_content="unsupported raw text",
        entity_type=EntityType.EPISODE.value,
        principal_id="owner-1",
        memory_scope="organization",
    )

    assert (
        raw_capture_visible_to_reader(
            capture,
            ctx=_reader_ctx(),
            accessible_projects=set(),
        )
        is False
    )
