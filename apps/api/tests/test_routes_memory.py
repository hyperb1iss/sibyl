from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.memory import (
    add_memory_space_member_record,
    apply_memory_correction_route,
    auto_review_reflection_candidate,
    create_memory_space_record,
    drain_reflection_review,
    get_memory_source_import_status,
    get_memory_space_record,
    inspect_memory_source,
    list_memory_audit,
    list_memory_space_records,
    preview_memory_correction_route,
    preview_memory_share_route,
    preview_memory_space_member_access,
    preview_reflection_promotion,
    promote_reflection_candidate,
    recall_raw,
    remember_raw,
    update_memory_space_record,
)
from sibyl.api.schemas import (
    MemoryCorrectionRequest,
    MemorySharePreviewRequest,
    MemorySpaceAccessPreviewRequest,
    MemorySpaceCreateRequest,
    MemorySpaceMemberCreateRequest,
    MemorySpaceUpdateRequest,
    RawMemoryRecallRequest,
    RawMemoryRememberRequest,
    ReflectionAutonomyRequest,
    ReflectionPromotionRequest,
    ReflectionReviewDrainRequest,
)
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.jobs import source_imports
from sibyl.services.recall_limits import RecallConcurrencyLimitExceededError
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.services.memory import (
    MemoryAccessPreview,
    MemoryCorrectionPreview,
    MemoryCorrectionResult,
    MemorySharePreview,
    ReflectionPromotionPreview,
    ReflectionPromotionResult,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


def _org() -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    return org


def _ctx(
    *,
    user_id: str = "user-123",
    org_role: OrganizationRole = OrganizationRole.MEMBER,
) -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.organization_id = "org-1"
    ctx.org_role = org_role
    return ctx


def _http_request() -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"user-agent": "SibylTest/1.0"},
    )


def _memory(**overrides: object) -> RawMemory:
    values = {
        "id": "memory-1",
        "organization_id": "org-1",
        "source_id": "cli:manual",
        "principal_id": "user-123",
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "title": "Raw note",
        "raw_content": "Sibyl stores raw memory before reflection.",
        "tags": ["memory"],
        "metadata": {"domain": "sibyl"},
        "provenance": {"message_id": "msg-1"},
        "capture_surface": "cli",
        "captured_at": datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        "created_at": datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC),
        "score": 0.5,
    }
    values.update(overrides)
    return RawMemory(**values)


def _space(**overrides: object) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "memory_scope": "private",
        "scope_key": "user-123",
        "name": "Private memory",
        "description": "Personal source truth",
        "state": "active",
        "disabled_reason": None,
        "metadata": {"source": "test"},
        "created_by_user_id": uuid4(),
        "created_at": datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _space_member(**overrides: object) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "space_id": uuid4(),
        "principal_type": "user",
        "principal_id": "user-123",
        "role": "reader",
        "permissions": ["read"],
        "expires_at": None,
        "created_by_user_id": uuid4(),
        "created_at": datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _clear_source_imports() -> Iterator[None]:
    source_imports.clear_source_import_runs()
    yield
    source_imports.clear_source_import_runs()


@pytest.mark.asyncio
async def test_memory_source_import_status_returns_source_safe_progress() -> None:
    org = _org()
    run = source_imports.SourceImportRun(
        import_id="source_import:test",
        organization_id=str(org.id),
        principal_id="user-123",
        source_uri="/private/mailbox.mbox",
        adapter_name="mbox",
        options={},
        policy_context={
            "actor_user_id": "user-123",
            "organization_id": str(org.id),
            "organization_role": "member",
            "memory_space": "private",
            "scope_key": None,
            "source_surface": "source_import",
        },
        batch_size=1,
        promotion_preview_approved=False,
        status=source_imports.SourceImportStatus.PAUSED,
        imported_count=1,
        skipped_count=2,
        dedupe_count=1,
    )
    source_imports._SOURCE_IMPORT_RUNS[run.import_id] = run

    response = await get_memory_source_import_status(run.import_id, org=org, ctx=_ctx())

    assert response.import_id == run.import_id
    assert response.status == "paused"
    assert response.progress.imported_count == 1
    assert response.progress.skipped_count == 2
    assert response.progress.dedupe_count == 1
    assert not hasattr(response, "source_uri")


@pytest.mark.asyncio
async def test_remember_raw_uses_current_org_and_principal() -> None:
    org = _org()
    http_request = _http_request()
    with (
        patch(
            "sibyl.api.routes.memory.remember_raw_memory",
            AsyncMock(return_value=_memory(organization_id=str(org.id), source_id="source-1")),
        ) as remember,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await remember_raw(
            RawMemoryRememberRequest(
                title="Raw note",
                raw_content="Sibyl stores raw memory before reflection.",
                source_id="source-1",
                tags=["memory"],
                provenance={"message_id": "msg-1"},
                capture_surface="cli",
            ),
            http_request=http_request,
            org=org,
            ctx=_ctx(),
        )

    remember.assert_awaited_once_with(
        organization_id=str(org.id),
        principal_id="user-123",
        source_id="source-1",
        raw_content="Sibyl stores raw memory before reflection.",
        title="Raw note",
        memory_scope="private",
        scope_key=None,
        tags=["memory"],
        metadata={},
        provenance={"message_id": "msg-1"},
        capture_surface="cli",
    )
    audit.assert_awaited_once_with(
        action="memory.remember",
        user_id="user-123",
        organization_id="org-1",
        request=http_request,
        memory_scope="private",
        scope_key=None,
        project_id=None,
        source_surface="cli",
        source_ids=["source-1"],
        derived_ids=["memory-1"],
        policy_allowed=True,
        policy_reason="same_scope_write_allowed",
        details={"agent_id": None, "diary": False, "tag_count": 1},
    )
    assert response.id == "memory-1"
    assert response.source_id == "source-1"
    assert response.principal_id == "user-123"
    assert response.policy_reason == "same_scope_write_allowed"


@pytest.mark.asyncio
async def test_remember_raw_audits_project_filter_denial() -> None:
    org = _org()
    ctx = _ctx()
    http_request = _http_request()
    denial = HTTPException(status_code=403, detail="project_access_denied")
    with (
        patch(
            "sibyl.api.routes.memory.verify_entity_project_access",
            AsyncMock(side_effect=denial),
        ) as verify,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
        patch("sibyl.api.routes.memory.remember_raw_memory", AsyncMock()) as remember,
        pytest.raises(HTTPException) as exc,
    ):
        await remember_raw(
            RawMemoryRememberRequest(
                title="Project note",
                raw_content="Should not write without project access.",
                project_id="project_123",
            ),
            http_request=http_request,
            org=org,
            ctx=ctx,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "project_access_denied"
    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    audit.assert_awaited_once_with(
        action="memory.policy_deny",
        user_id="user-123",
        organization_id="org-1",
        request=http_request,
        memory_scope="private",
        scope_key=None,
        project_id="project_123",
        source_surface="raw_remember",
        source_ids=None,
        derived_ids=None,
        policy_allowed=False,
        policy_reason="project_access_denied",
        details={
            "policy_action": "write",
            "required_project_role": "project_contributor",
        },
    )
    remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_raw_diary_sets_agent_metadata_and_surface() -> None:
    org = _org()
    ctx = _ctx()
    with (
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()) as verify,
        patch(
            "sibyl.api.routes.memory.remember_raw_memory",
            AsyncMock(
                return_value=_memory(
                    organization_id=str(org.id),
                    source_id="agent_diary:manual",
                    capture_surface="agent_diary",
                    metadata={
                        "agent_id": "nova",
                        "memory_kind": "agent_diary",
                        "project_id": "project_123",
                    },
                )
            ),
        ) as remember,
    ):
        response = await remember_raw(
            RawMemoryRememberRequest(
                title="Nova diary",
                raw_content="Keep track of private implementation state.",
                diary=True,
                agent_id="nova",
                project_id="project_123",
            ),
            org=org,
            ctx=ctx,
        )

    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    remember.assert_awaited_once_with(
        organization_id=str(org.id),
        principal_id="user-123",
        source_id="agent_diary:manual",
        raw_content="Keep track of private implementation state.",
        title="Nova diary",
        memory_scope="private",
        scope_key=None,
        tags=[],
        metadata={
            "agent_id": "nova",
            "memory_kind": "agent_diary",
            "project_id": "project_123",
        },
        provenance={},
        capture_surface="agent_diary",
    )
    assert response.metadata["agent_id"] == "nova"
    assert response.capture_surface == "agent_diary"
    assert response.policy_reason == "same_scope_write_allowed"


@pytest.mark.asyncio
async def test_remember_raw_diary_requires_agent_id() -> None:
    with (
        patch("sibyl.api.routes.memory.remember_raw_memory", AsyncMock()) as remember,
        pytest.raises(HTTPException) as exc,
    ):
        await remember_raw(
            RawMemoryRememberRequest(raw_content="private state", diary=True),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 400
    remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_raw_diary_requires_private_scope() -> None:
    with (
        patch("sibyl.api.routes.memory.remember_raw_memory", AsyncMock()) as remember,
        pytest.raises(HTTPException) as exc,
    ):
        await remember_raw(
            RawMemoryRememberRequest(
                raw_content="private state",
                diary=True,
                agent_id="nova",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 400
    remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_raw_defaults_source_id_from_surface() -> None:
    org = _org()
    with patch(
        "sibyl.api.routes.memory.remember_raw_memory",
        AsyncMock(return_value=_memory(source_id="api:manual")),
    ) as remember:
        await remember_raw(
            RawMemoryRememberRequest(raw_content="small note"),
            org=org,
            ctx=_ctx(),
        )

    assert remember.await_args.kwargs["source_id"] == "api:manual"


@pytest.mark.asyncio
async def test_remember_raw_uses_shared_policy_for_project_scope_write() -> None:
    org = _org()
    ctx = _ctx()
    with (
        patch(
            "sibyl.api.routes.memory.verify_entity_project_access",
            AsyncMock(),
        ) as verify_access,
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_123"}),
        ) as accessible_projects,
        patch("sibyl.api.routes.memory.log") as route_log,
        patch(
            "sibyl.api.routes.memory.remember_raw_memory",
            AsyncMock(return_value=_memory(organization_id=str(org.id), scope_key="project_123")),
        ),
    ):
        response = await remember_raw(
            RawMemoryRememberRequest(
                raw_content="project note",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=org,
            ctx=ctx,
        )

    verify_access.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    accessible_projects.assert_awaited_once_with(ctx)
    route_log.info.assert_any_call(
        "memory_policy_decision",
        action="write",
        allowed=True,
        memory_scope="project",
        organization_id="org-1",
        policy_reason="same_scope_write_allowed",
        principal_id="user-123",
        scope_key="project_123",
        surface="raw_remember",
    )
    assert response.policy_reason == "same_scope_write_allowed"


@pytest.mark.asyncio
async def test_remember_raw_denies_disallowed_api_key_memory_space() -> None:
    ctx = _ctx()
    ctx.api_key_memory_scope_keys = [api_key_memory_scope_key("project", "project_allowed")]
    with (
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_123"}),
        ),
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
        patch("sibyl.api.routes.memory.remember_raw_memory", AsyncMock()) as remember,
        pytest.raises(HTTPException) as exc,
    ):
        await remember_raw(
            RawMemoryRememberRequest(
                raw_content="project note",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=_org(),
            ctx=ctx,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "api_key_memory_space_denied"
    remember.assert_not_awaited()
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["policy_reason"] == "api_key_memory_space_denied"


@pytest.mark.asyncio
async def test_remember_raw_denies_project_scope_without_policy_membership() -> None:
    http_request = _http_request()
    with (
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_other"}),
        ),
        patch("sibyl.api.routes.memory.log") as route_log,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
        patch("sibyl.api.routes.memory.remember_raw_memory", AsyncMock()) as remember,
        pytest.raises(HTTPException) as exc,
    ):
        await remember_raw(
            RawMemoryRememberRequest(
                raw_content="project note",
                memory_scope="project",
                scope_key="project_123",
            ),
            http_request=http_request,
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "unverified_membership"
    audit.assert_awaited_once_with(
        action="memory.policy_deny",
        user_id="user-123",
        organization_id="org-1",
        request=http_request,
        memory_scope="project",
        scope_key="project_123",
        project_id=None,
        source_surface="raw_remember",
        source_ids=None,
        derived_ids=None,
        policy_allowed=False,
        policy_reason="unverified_membership",
        details={"policy_action": "write"},
    )
    route_log.info.assert_any_call(
        "memory_policy_decision",
        action="write",
        allowed=False,
        memory_scope="project",
        organization_id="org-1",
        policy_reason="unverified_membership",
        principal_id="user-123",
        scope_key="project_123",
        surface="raw_remember",
    )
    remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_raw_denies_project_scope_without_contributor_role() -> None:
    with (
        patch(
            "sibyl.api.routes.memory.verify_entity_project_access",
            AsyncMock(side_effect=HTTPException(status_code=403, detail="project_access_denied")),
        ),
        patch("sibyl.api.routes.memory.remember_raw_memory", AsyncMock()) as remember,
        pytest.raises(HTTPException) as exc,
    ):
        await remember_raw(
            RawMemoryRememberRequest(
                raw_content="project note",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "project_access_denied"
    remember.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_raw_returns_scoped_memories() -> None:
    org = _org()
    http_request = _http_request()
    with (
        patch(
            "sibyl.api.routes.memory.recall_raw_memory",
            AsyncMock(return_value=[_memory(organization_id=str(org.id))]),
        ) as recall,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await recall_raw(
            RawMemoryRecallRequest(query="raw memory", limit=5),
            http_request=http_request,
            org=org,
            ctx=_ctx(),
        )

    recall.assert_awaited_once_with(
        organization_id=str(org.id),
        principal_id="user-123",
        query="raw memory",
        memory_scope="private",
        scope_key=None,
        agent_id=None,
        project_id=None,
        limit=5,
    )
    audit.assert_awaited_once_with(
        action="memory.recall",
        user_id="user-123",
        organization_id="org-1",
        request=http_request,
        memory_scope="private",
        scope_key=None,
        project_id=None,
        source_surface="raw_recall",
        source_ids=["cli:manual"],
        derived_ids=["memory-1"],
        policy_allowed=True,
        policy_reason="private_principal_bound",
        details={"agent_id": None, "diary": False, "limit": 5, "result_count": 1},
    )
    assert response.query == "raw memory"
    assert response.limit == 5
    assert response.policy_reason == "private_principal_bound"
    assert [memory.id for memory in response.memories] == ["memory-1"]
    assert response.memories[0].policy_reason == "private_principal_bound"


@pytest.mark.asyncio
async def test_recall_raw_forwards_import_metadata_filters() -> None:
    org = _org()
    http_request = _http_request()
    occurred_after = datetime(2014, 1, 1, tzinfo=UTC)
    occurred_before = datetime(2014, 12, 31, 23, 59, 59, tzinfo=UTC)
    with (
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock(return_value=[])) as recall,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        await recall_raw(
            RawMemoryRecallRequest(
                query="surrealdb",
                participants=["nova@example.com"],
                labels=["email"],
                thread_id="thread-1",
                occurred_after=occurred_after,
                occurred_before=occurred_before,
                limit=5,
            ),
            http_request=http_request,
            org=org,
            ctx=_ctx(),
        )

    recall.assert_awaited_once_with(
        organization_id=str(org.id),
        principal_id="user-123",
        query="surrealdb",
        memory_scope="private",
        scope_key=None,
        agent_id=None,
        project_id=None,
        limit=5,
        participants=["nova@example.com"],
        labels=["email"],
        thread_id="thread-1",
        occurred_after=occurred_after,
        occurred_before=occurred_before,
    )
    audit_details = audit.await_args.kwargs["details"]
    assert audit_details["participants"] == ["nova@example.com"]
    assert audit_details["labels"] == ["email"]
    assert audit_details["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_recall_raw_rate_limits_concurrent_member_recall() -> None:
    with (
        patch(
            "sibyl.api.routes.memory.recall_concurrency_slot",
            side_effect=RecallConcurrencyLimitExceededError(
                user_id="user-123",
                max_concurrent=3,
            ),
        ),
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock()) as recall,
        pytest.raises(HTTPException) as exc,
    ):
        await recall_raw(
            RawMemoryRecallRequest(query="raw memory", limit=5),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 429
    assert exc.value.detail == {
        "error": "recall_concurrency_limit_exceeded",
        "max_concurrent": 3,
    }
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_raw_diary_filters_agent_and_project() -> None:
    org = _org()
    ctx = _ctx()
    with (
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()) as verify,
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock(return_value=[])) as recall,
    ):
        response = await recall_raw(
            RawMemoryRecallRequest(
                query="implementation state",
                diary=True,
                agent_id="nova",
                project_id="project_123",
            ),
            org=org,
            ctx=ctx,
        )

    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.VIEWER,
        require_existing_project=True,
    )
    recall.assert_awaited_once_with(
        organization_id=str(org.id),
        principal_id="user-123",
        query="implementation state",
        memory_scope="private",
        scope_key=None,
        agent_id="nova",
        project_id="project_123",
        limit=10,
    )
    assert response.policy_reason == "agent_diary_private_read_allowed"


@pytest.mark.asyncio
async def test_recall_raw_diary_requires_agent_id() -> None:
    with (
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock()) as recall,
        pytest.raises(HTTPException) as exc,
    ):
        await recall_raw(
            RawMemoryRecallRequest(query="implementation state", diary=True),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 400
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_raw_diary_requires_private_scope() -> None:
    with (
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock()) as recall,
        pytest.raises(HTTPException) as exc,
    ):
        await recall_raw(
            RawMemoryRecallRequest(
                query="implementation state",
                diary=True,
                agent_id="nova",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 400
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_raw_uses_shared_policy_for_project_scope_read() -> None:
    org = _org()
    ctx = _ctx()
    with (
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_123"}),
        ) as accessible_projects,
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock(return_value=[])),
    ):
        response = await recall_raw(
            RawMemoryRecallRequest(
                query="project memory",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=org,
            ctx=ctx,
        )

    accessible_projects.assert_awaited_once_with(ctx)
    assert response.policy_reason == "project_access_verified"


@pytest.mark.asyncio
async def test_recall_raw_allows_matching_api_key_memory_space() -> None:
    org = _org()
    ctx = _ctx()
    ctx.api_key_memory_scope_keys = [api_key_memory_scope_key("project", "project_123")]
    with (
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_123"}),
        ),
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock(return_value=[])) as recall,
    ):
        response = await recall_raw(
            RawMemoryRecallRequest(
                query="project memory",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=org,
            ctx=ctx,
        )

    recall.assert_awaited_once()
    assert response.policy_reason == "project_access_verified"


@pytest.mark.asyncio
async def test_recall_raw_blocks_keyed_team_scope_for_non_admin() -> None:
    with (
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock()) as recall,
        pytest.raises(HTTPException) as exc,
    ):
        await recall_raw(
            RawMemoryRecallRequest(query="team memory", memory_scope="team", scope_key="team_123"),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "scope_not_enabled"
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_raw_maps_scope_errors_to_400() -> None:
    with (
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock()) as recall,
        pytest.raises(HTTPException) as exc,
    ):
        await recall_raw(
            RawMemoryRecallRequest(query="raw memory", memory_scope="project", limit=5),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "missing_scope_key"
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_memory_space_records_returns_disabled_reason() -> None:
    org = _org()
    space = _space(
        organization_id=org.id,
        memory_scope="team",
        state="disabled",
        disabled_reason="scope_not_enabled",
    )

    with patch(
        "sibyl.api.routes.memory.list_memory_spaces",
        AsyncMock(return_value=[space]),
    ) as list_spaces:
        response = await list_memory_space_records(org=org)

    list_spaces.assert_awaited_once_with(organization_id=org.id)
    assert len(response.spaces) == 1
    assert response.spaces[0].memory_scope == "team"
    assert response.spaces[0].state == "disabled"
    assert response.spaces[0].disabled_reason == "scope_not_enabled"
    assert response.spaces[0].members == []


@pytest.mark.asyncio
async def test_create_memory_space_record_uses_authenticated_actor() -> None:
    org = _org()
    actor_id = uuid4()
    space = _space(
        organization_id=org.id,
        created_by_user_id=actor_id,
        memory_scope="project",
        scope_key="project_alpha",
        name="Project memory",
    )

    with patch(
        "sibyl.api.routes.memory.create_memory_space",
        AsyncMock(return_value=space),
    ) as create_space:
        response = await create_memory_space_record(
            MemorySpaceCreateRequest(
                memory_scope="project",
                scope_key="project_alpha",
                name="Project memory",
                metadata={"kind": "roadmap"},
            ),
            org=org,
            ctx=_ctx(user_id=str(actor_id), org_role=OrganizationRole.OWNER),
        )

    create_space.assert_awaited_once_with(
        organization_id=org.id,
        created_by_user_id=actor_id,
        memory_scope="project",
        scope_key="project_alpha",
        name="Project memory",
        description=None,
        metadata={"kind": "roadmap"},
    )
    assert response.id == str(space.id)
    assert response.created_by_user_id == str(actor_id)


@pytest.mark.asyncio
async def test_get_memory_space_record_includes_memberships() -> None:
    org = _org()
    space_id = uuid4()
    space = _space(organization_id=org.id, id=space_id)
    member = _space_member(
        organization_id=org.id,
        space_id=space_id,
        principal_type="agent",
        principal_id="agent:nova",
        role="reader",
    )

    with (
        patch(
            "sibyl.api.routes.memory.get_memory_space",
            AsyncMock(return_value=space),
        ) as get_space,
        patch(
            "sibyl.api.routes.memory.list_memory_space_members",
            AsyncMock(return_value=[member]),
        ) as list_members,
    ):
        response = await get_memory_space_record(space_id, org=org)

    get_space.assert_awaited_once_with(organization_id=org.id, space_id=space_id)
    list_members.assert_awaited_once_with(organization_id=org.id, space_id=space_id)
    assert response.id == str(space_id)
    assert response.members[0].principal_type == "agent"
    assert response.members[0].principal_id == "agent:nova"


@pytest.mark.asyncio
async def test_update_memory_space_record_returns_memberships() -> None:
    org = _org()
    space_id = uuid4()
    space = _space(organization_id=org.id, id=space_id, name="Renamed memory")
    member = _space_member(organization_id=org.id, space_id=space_id)

    with (
        patch(
            "sibyl.api.routes.memory.update_memory_space",
            AsyncMock(return_value=space),
        ) as update_space,
        patch(
            "sibyl.api.routes.memory.list_memory_space_members",
            AsyncMock(return_value=[member]),
        ) as list_members,
    ):
        response = await update_memory_space_record(
            space_id,
            MemorySpaceUpdateRequest(name="Renamed memory", metadata={"fresh": True}),
            org=org,
        )

    update_space.assert_awaited_once_with(
        organization_id=org.id,
        space_id=space_id,
        name="Renamed memory",
        description=None,
        state=None,
        metadata={"fresh": True},
    )
    list_members.assert_awaited_once_with(organization_id=org.id, space_id=space_id)
    assert response.name == "Renamed memory"
    assert response.members[0].principal_id == "user-123"


@pytest.mark.asyncio
async def test_add_memory_space_member_record_returns_grant() -> None:
    org = _org()
    actor_id = uuid4()
    space_id = uuid4()
    member = _space_member(
        organization_id=org.id,
        space_id=space_id,
        principal_type="agent",
        principal_id="agent:nova",
        created_by_user_id=actor_id,
    )

    with patch(
        "sibyl.api.routes.memory.add_memory_space_member",
        AsyncMock(return_value=member),
    ) as add_member:
        response = await add_memory_space_member_record(
            space_id,
            MemorySpaceMemberCreateRequest(
                principal_type="agent",
                principal_id="agent:nova",
                permissions=["read"],
            ),
            org=org,
            ctx=_ctx(user_id=str(actor_id), org_role=OrganizationRole.OWNER),
        )

    add_member.assert_awaited_once_with(
        organization_id=org.id,
        space_id=space_id,
        created_by_user_id=actor_id,
        principal_type="agent",
        principal_id="agent:nova",
        role="reader",
        permissions=["read"],
        expires_at=None,
    )
    assert response.principal_type == "agent"
    assert response.principal_id == "agent:nova"


@pytest.mark.asyncio
async def test_preview_memory_space_member_access_audits_agent_visibility() -> None:
    org = _org()
    actor_id = uuid4()
    space_id = uuid4()
    space = _space(
        organization_id=org.id,
        id=space_id,
        memory_scope="project",
        scope_key="project_alpha",
    )
    result = MemoryAccessPreview(
        allowed=True,
        reason="access_preview_allowed",
        target_principal_type="agent",
        target_principal_id="agent:nova",
        memory_space_ids=[str(space_id)],
        visible_source_ids=["raw-1"],
        denied_source_ids=[],
        missing_source_ids=[],
        redacted_count=0,
        hidden_but_relevant_count=0,
        metadata={"policy_reasons": ["project_access_verified"]},
    )

    with (
        patch(
            "sibyl.api.routes.memory.get_memory_space",
            AsyncMock(return_value=space),
        ) as get_space,
        patch(
            "sibyl.api.routes.memory.preview_memory_access",
            AsyncMock(return_value=result),
        ) as preview_access,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await preview_memory_space_member_access(
            space_id,
            MemorySpaceAccessPreviewRequest(
                target_principal_type="agent",
                target_principal_id="agent:nova",
                limit=25,
            ),
            http_request=_http_request(),
            org=org,
            ctx=_ctx(user_id=str(actor_id), org_role=OrganizationRole.OWNER),
        )

    get_space.assert_awaited_once_with(organization_id=org.id, space_id=space_id)
    preview_access.assert_awaited_once_with(
        organization_id=str(org.id),
        actor_user_id=str(actor_id),
        target_principal_type="agent",
        target_principal_id="agent:nova",
        memory_spaces=[space],
        limit=25,
    )
    audit.assert_awaited_once()
    assert response.allowed is True
    assert response.visible_source_ids == ["raw-1"]
    assert response.memory_space_ids == [str(space_id)]
    assert response.policy_reasons == ["project_access_verified"]


@pytest.mark.asyncio
async def test_list_memory_audit_returns_inspectable_events() -> None:
    org = _org()
    created_at = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    with patch(
        "sibyl.api.routes.memory.list_memory_audit_events",
        AsyncMock(
            return_value=[
                {
                    "uuid": "audit-1",
                    "organization_id": str(org.id),
                    "user_id": "user-123",
                    "action": "memory.remember",
                    "details": {
                        "memory_scope": "project",
                        "scope_key": "project_123",
                        "project_id": "project_123",
                        "source_surface": "cli",
                        "source_ids": ["source-1"],
                        "source_ids_truncated": 2,
                        "derived_ids": ["memory-1"],
                        "policy_allowed": True,
                        "policy_reason": "same_scope_write_allowed",
                        "details": {"tag_count": 1},
                    },
                    "created_at": created_at,
                }
            ]
        ),
    ) as list_events:
        response = await list_memory_audit(
            org=org,
            action="memory.remember",
            actor_user_id="user-123",
            source_id="source-1",
            derived_id=None,
            memory_scope="project",
            project_id="project_123",
            policy_allowed=True,
            limit=25,
        )

    list_events.assert_awaited_once_with(
        organization_id=org.id,
        user_id="user-123",
        action="memory.remember",
        source_id="source-1",
        derived_id=None,
        memory_scope="project",
        project_id="project_123",
        policy_allowed=True,
        limit=25,
    )
    assert response.limit == 25
    event = response.events[0]
    assert event.id == "audit-1"
    assert event.organization_id == str(org.id)
    assert event.user_id == "user-123"
    assert event.action == "memory.remember"
    assert event.memory_scope == "project"
    assert event.scope_key == "project_123"
    assert event.source_ids == ["source-1"]
    assert event.source_ids_truncated == 2
    assert event.derived_ids == ["memory-1"]
    assert event.policy_allowed is True
    assert event.policy_reason == "same_scope_write_allowed"
    assert event.details == {"tag_count": 1}
    assert event.created_at == created_at


@pytest.mark.asyncio
async def test_list_memory_audit_rejects_non_memory_action() -> None:
    with pytest.raises(HTTPException) as exc:
        await list_memory_audit(
            org=_org(),
            action="auth.login",
            actor_user_id=None,
            source_id=None,
            derived_id=None,
            memory_scope=None,
            project_id=None,
            policy_allowed=None,
            limit=25,
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_memory_audit_action"


@pytest.mark.asyncio
async def test_inspect_memory_source_returns_metadata_and_visible_content() -> None:
    org = _org()
    remember_created_at = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    promote_created_at = datetime(2026, 5, 13, 12, 1, 0, tzinfo=UTC)
    memory = _memory(
        id="memory-1",
        organization_id=str(org.id),
        source_id="source-1",
        entity_type="procedure",
        review_state="promoted",
        project_id="project_123",
        metadata={
            "domain": "sibyl",
            "correction_history": [{"action": "mark_stale", "reason": "outdated"}],
            "promoted_entity_id": "entity-1",
            "promoted_at": "2026-05-13T12:02:00+00:00",
            "transform_version": "native-v1",
            "memory_lifecycle": {
                "state": "promoted",
                "source_id": "memory-1",
                "action": "promote",
                "reason": "accepted",
                "derived_ids": ["entity-1"],
            },
            "reflection_findings": [
                {
                    "kind": "promotion",
                    "target_source_id": "memory-1",
                    "reason": "accepted",
                    "related_source_ids": ["entity-1"],
                }
            ],
            "claim_records": [
                {
                    "content": "Sibyl stores raw memory before reflection.",
                    "confidence": 0.91,
                    "source_ids": ["memory-1"],
                }
            ],
        },
    )
    remember_event = {
        "uuid": "audit-1",
        "organization_id": str(org.id),
        "user_id": "user-123",
        "action": "memory.remember",
        "details": {
            "memory_scope": "private",
            "source_ids": ["source-1"],
            "derived_ids": ["memory-1"],
            "policy_allowed": True,
            "policy_reason": "private_principal_bound",
        },
        "created_at": remember_created_at,
    }
    promote_event = {
        "uuid": "audit-2",
        "organization_id": str(org.id),
        "user_id": "user-123",
        "action": "memory.reflect.promote",
        "details": {
            "memory_scope": "project",
            "source_ids": ["memory-1"],
            "derived_ids": ["entity-1"],
            "policy_allowed": True,
            "policy_reason": "promotion_allowed",
        },
        "created_at": promote_created_at,
    }

    async def list_events(**kwargs: object) -> list[dict[str, object]]:
        if kwargs.get("source_id") == "memory-1":
            return [promote_event]
        if kwargs.get("source_id") == "source-1":
            return [remember_event]
        if kwargs.get("derived_id") == "memory-1":
            return [remember_event]
        return []

    with (
        patch("sibyl.api.routes.memory.get_raw_memory", AsyncMock(return_value=memory)) as get_raw,
        patch(
            "sibyl.api.routes.memory.get_raw_memory_by_source_id",
            AsyncMock(),
        ) as get_by_source,
        patch(
            "sibyl.api.routes.memory.list_memory_audit_events",
            AsyncMock(side_effect=list_events),
        ) as audit_events,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await inspect_memory_source(
            "memory-1",
            http_request=_http_request(),
            org=org,
            ctx=_ctx(org_role=OrganizationRole.OWNER),
        )

    get_raw.assert_awaited_once_with(organization_id=str(org.id), memory_id="memory-1")
    get_by_source.assert_not_awaited()
    assert audit_events.await_count == 3
    assert response.id == "memory-1"
    assert response.source_id == "source-1"
    assert response.project_id == "project_123"
    assert response.raw_content == "Sibyl stores raw memory before reflection."
    assert response.content_redacted is False
    assert response.policy_allowed is True
    assert response.policy_reason == "private_principal_bound"
    assert response.visibility["content_visible"] is True
    assert response.visibility["project_id"] == "project_123"
    assert response.lifecycle["state"] == "promoted"
    assert response.lifecycle["derived_ids"] == ["entity-1"]
    assert response.reflection_findings[0]["kind"] == "promotion"
    assert response.reflection_findings[0]["related_source_ids"] == ["entity-1"]
    assert response.claim_records[0]["content"] == "Sibyl stores raw memory before reflection."
    assert response.correction_history == [{"action": "mark_stale", "reason": "outdated"}]
    assert response.promotion_state["state"] == "promoted"
    assert response.promotion_state["promoted_id"] == "entity-1"
    assert response.share_state == {"state": "none", "audit_event_ids": []}
    assert response.transform_versions == {"transform_version": "native-v1"}
    assert response.available_actions[0] == {
        "action": "inspect",
        "available": True,
        "preview_required": False,
    }
    assert response.derived_ids == ["entity-1", "memory-1"]
    assert response.derived_types == ["graph_entity", "raw_memory"]
    assert response.audit_event_count == 2
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "memory.inspect"
    assert audit.await_args.kwargs["policy_allowed"] is True


@pytest.mark.asyncio
async def test_inspect_memory_source_redacts_project_content_without_access() -> None:
    org = _org()
    memory = _memory(
        id="memory-1",
        organization_id=str(org.id),
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_hidden",
        project_id="project_hidden",
        raw_content="Private project detail.",
        metadata={
            "domain": "sibyl",
            "memory_lifecycle": {"state": "promoted"},
            "reflection_findings": [{"kind": "promotion"}],
            "claim_records": [{"content": "Private project detail."}],
        },
    )
    event = {
        "uuid": "audit-1",
        "organization_id": str(org.id),
        "user_id": "user-123",
        "action": "memory.recall",
        "details": {
            "memory_scope": "project",
            "source_ids": ["cli:manual"],
            "derived_ids": ["memory-1"],
            "policy_allowed": True,
            "details": {"content_preview": "Private project detail."},
        },
        "created_at": datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC),
    }

    with (
        patch("sibyl.api.routes.memory.get_raw_memory", AsyncMock(return_value=memory)),
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ) as accessible_projects,
        patch(
            "sibyl.api.routes.memory.list_memory_audit_events",
            AsyncMock(return_value=[event]),
        ),
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await inspect_memory_source(
            "memory-1",
            http_request=_http_request(),
            org=org,
            ctx=_ctx(org_role=OrganizationRole.ADMIN),
        )

    accessible_projects.assert_awaited_once()
    assert response.raw_content is None
    assert response.content_redacted is True
    assert response.raw_content_length == len("Private project detail.")
    assert response.policy_allowed is False
    assert response.policy_reason == "unverified_membership"
    assert response.visibility["content_visible"] is False
    assert response.available_actions[1]["available"] is False
    assert response.metadata == {"domain": "sibyl"}
    assert response.lifecycle == {}
    assert response.reflection_findings == []
    assert response.claim_records == []
    assert response.recent_audit_events[0].details == {}
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["policy_allowed"] is False
    assert audit.await_args.kwargs["policy_reason"] == "unverified_membership"


@pytest.mark.asyncio
async def test_inspect_memory_source_redacts_other_private_principal() -> None:
    org = _org()
    memory = _memory(
        id="memory-1",
        organization_id=str(org.id),
        principal_id="other-user",
        raw_content="Another user's private note.",
        metadata={
            "domain": "sibyl",
            "memory_lifecycle": {"state": "active"},
            "reflection_findings": [{"kind": "correction"}],
            "claim_records": [{"content": "Another user's private note."}],
        },
    )

    with (
        patch("sibyl.api.routes.memory.get_raw_memory", AsyncMock(return_value=memory)),
        patch(
            "sibyl.api.routes.memory.list_memory_audit_events",
            AsyncMock(return_value=[]),
        ),
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await inspect_memory_source(
            "memory-1",
            http_request=_http_request(),
            org=org,
            ctx=_ctx(user_id="user-123", org_role=OrganizationRole.OWNER),
        )

    assert response.raw_content is None
    assert response.content_redacted is True
    assert response.policy_allowed is False
    assert response.policy_reason == "principal_mismatch"
    assert response.metadata == {"domain": "sibyl"}
    assert response.lifecycle == {}
    assert response.reflection_findings == []
    assert response.claim_records == []
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["policy_reason"] == "principal_mismatch"


@pytest.mark.asyncio
async def test_inspect_memory_source_can_lookup_by_provenance_source_id() -> None:
    org = _org()
    memory = _memory(
        id="memory-1",
        organization_id=str(org.id),
        source_id="source/provenance",
    )

    with (
        patch("sibyl.api.routes.memory.get_raw_memory", AsyncMock(return_value=None)) as get_raw,
        patch(
            "sibyl.api.routes.memory.get_raw_memory_by_source_id",
            AsyncMock(return_value=memory),
        ) as get_by_source,
        patch(
            "sibyl.api.routes.memory.list_memory_audit_events",
            AsyncMock(return_value=[]),
        ),
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()),
    ):
        response = await inspect_memory_source(
            "source/provenance",
            http_request=_http_request(),
            org=org,
            ctx=_ctx(org_role=OrganizationRole.OWNER),
        )

    get_raw.assert_awaited_once_with(
        organization_id=str(org.id),
        memory_id="source/provenance",
    )
    get_by_source.assert_awaited_once_with(
        organization_id=str(org.id),
        source_id="source/provenance",
    )
    assert response.id == "memory-1"
    assert response.source_id == "source/provenance"


@pytest.mark.asyncio
async def test_inspect_memory_source_returns_404_for_missing_source() -> None:
    with (
        patch("sibyl.api.routes.memory.get_raw_memory", AsyncMock(return_value=None)),
        patch(
            "sibyl.api.routes.memory.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await inspect_memory_source(
            "missing-memory",
            http_request=_http_request(),
            org=_org(),
            ctx=_ctx(org_role=OrganizationRole.OWNER),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "memory_source_not_found"


@pytest.mark.asyncio
async def test_preview_memory_correction_audits_lifecycle_action() -> None:
    org = _org()
    memory = _memory(id="memory-1", organization_id=str(org.id), source_id="source-1")
    preview = MemoryCorrectionPreview(
        allowed=True,
        source_id="memory-1",
        action="hide",
        reason="hide_preview_allowed",
        target_review_state="hidden",
        affected_source_ids=["memory-1"],
        affected_derived_ids=["entity-1"],
        reversible=True,
        recall_impact={"excluded_from_recall": True},
        synthesis_impact={"excluded_from_synthesis": True},
        audit_action="memory.correction.hide",
        metadata={"policy_reasons": ["private_principal_bound"]},
    )

    with (
        patch("sibyl.api.routes.memory.get_raw_memory", AsyncMock(return_value=memory)),
        patch(
            "sibyl.api.routes.memory.preview_memory_correction",
            AsyncMock(return_value=preview),
        ) as preview_call,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await preview_memory_correction_route(
            "memory-1",
            MemoryCorrectionRequest(action="hide", reason="outdated"),
            http_request=_http_request(),
            org=org,
            ctx=_ctx(org_role=OrganizationRole.OWNER),
        )

    assert response.allowed is True
    assert response.applied is False
    assert response.audit_action == "memory.correction.hide"
    assert response.affected_derived_ids == ["entity-1"]
    preview_call.assert_awaited_once_with(
        organization_id=str(org.id),
        source_id="memory-1",
        principal_id="user-123",
        action="hide",
        reason="outdated",
        accessible_projects=None,
        replacement_source_id=None,
        duplicate_of_source_id=None,
    )
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "memory.correction.hide.preview"
    assert audit.await_args.kwargs["source_ids"] == ["memory-1"]


@pytest.mark.asyncio
async def test_apply_memory_correction_returns_updated_review_state() -> None:
    org = _org()
    memory = _memory(id="memory-1", organization_id=str(org.id), source_id="source-1")
    updated = _memory(
        id="memory-1",
        organization_id=str(org.id),
        source_id="source-1",
        review_state="hidden",
        metadata={
            "memory_lifecycle": {
                "state": "hidden",
                "source_id": "memory-1",
                "action": "hide",
                "reason": "outdated",
            },
            "reflection_findings": [
                {
                    "kind": "correction",
                    "target_source_id": "memory-1",
                    "reason": "outdated",
                    "action": "hide",
                    "lifecycle_state": "hidden",
                }
            ],
        },
    )
    preview = MemoryCorrectionPreview(
        allowed=True,
        source_id="memory-1",
        action="hide",
        reason="hidden",
        target_review_state="hidden",
        affected_source_ids=["memory-1"],
        affected_derived_ids=[],
        reversible=True,
        recall_impact={"excluded_from_recall": True},
        synthesis_impact={"excluded_from_synthesis": True},
        audit_action="memory.correction.hide",
        metadata={"policy_reasons": ["private_principal_bound"]},
    )
    result = MemoryCorrectionResult(applied=True, preview=preview, updated_memory=updated)

    with (
        patch("sibyl.api.routes.memory.get_raw_memory", AsyncMock(return_value=memory)),
        patch(
            "sibyl.api.routes.memory.apply_memory_correction",
            AsyncMock(return_value=result),
        ) as apply_call,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await apply_memory_correction_route(
            "memory-1",
            MemoryCorrectionRequest(action="hide", reason="outdated"),
            http_request=_http_request(),
            org=org,
            ctx=_ctx(org_role=OrganizationRole.OWNER),
        )

    assert response.applied is True
    assert response.updated_review_state == "hidden"
    assert response.lifecycle["state"] == "hidden"
    assert response.lifecycle["action"] == "hide"
    assert response.reflection_finding is not None
    assert response.reflection_finding["kind"] == "correction"
    apply_call.assert_awaited_once_with(
        organization_id=str(org.id),
        source_id="memory-1",
        principal_id="user-123",
        action="hide",
        reason="outdated",
        accessible_projects=None,
        replacement_source_id=None,
        duplicate_of_source_id=None,
    )
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "memory.correction.hide"
    assert audit.await_args.kwargs["policy_allowed"] is True


@pytest.mark.asyncio
async def test_preview_memory_share_returns_disabled_contract_and_audit() -> None:
    org = _org()
    ctx = _ctx()
    http_request = _http_request()
    result = MemorySharePreview(
        allowed=False,
        reason="scope_not_enabled",
        target_scope=MemoryScope.ORGANIZATION,
        target_scope_key=None,
        source_ids=["memory-1"],
        visible_source_ids=["memory-1"],
        denied_source_ids=[],
        missing_source_ids=[],
        redacted_count=0,
        hidden_but_relevant_count=0,
        metadata={
            "input_scopes": [
                {
                    "id": "memory-1",
                    "memory_scope": "private",
                    "scope_key": None,
                }
            ],
            "policy_reasons": [
                "scope_not_enabled",
                "private_principal_bound",
            ],
            "source_count": 1,
            "visible_count": 1,
        },
    )
    with (
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_123"}),
        ) as accessible,
        patch(
            "sibyl.api.routes.memory.preview_memory_share",
            AsyncMock(return_value=result),
        ) as preview,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await preview_memory_share_route(
            MemorySharePreviewRequest(
                source_ids=["memory-1"],
                target_scope="organization",
                recipient_organization_id="org-2",
            ),
            http_request=http_request,
            org=org,
            ctx=ctx,
        )

    accessible.assert_awaited_once_with(ctx)
    preview.assert_awaited_once_with(
        source_ids=["memory-1"],
        organization_id=str(org.id),
        principal_id="user-123",
        target_scope="organization",
        target_scope_key=None,
        recipient_organization_id="org-2",
        accessible_projects={"project_123"},
    )
    audit.assert_awaited_once_with(
        action="memory.share.preview",
        user_id="user-123",
        organization_id="org-1",
        request=http_request,
        memory_scope="organization",
        scope_key=None,
        project_id=None,
        source_surface="memory_share_preview",
        source_ids=["memory-1"],
        derived_ids=[],
        policy_allowed=False,
        policy_reason="scope_not_enabled",
        details={
            "denied_source_count": 0,
            "hidden_but_relevant_count": 0,
            "preview": True,
            "recipient_organization_id": "org-2",
            "redacted_count": 0,
            "target_scope": "organization",
            "visible_source_count": 1,
        },
    )
    assert response.allowed is False
    assert response.reason == "scope_not_enabled"
    assert response.target_scope == "organization"
    assert response.visible_source_ids == ["memory-1"]
    assert response.denied_source_ids == []
    assert response.missing_source_ids == []
    assert response.policy_reasons == [
        "scope_not_enabled",
        "private_principal_bound",
    ]
    assert response.input_scopes[0].id == "memory-1"


@pytest.mark.asyncio
async def test_preview_memory_share_requires_authenticated_user() -> None:
    ctx = _ctx()
    ctx.user_id = None

    with pytest.raises(HTTPException) as exc:
        await preview_memory_share_route(
            MemorySharePreviewRequest(
                source_ids=["memory-1"],
                target_scope="organization",
            ),
            http_request=_http_request(),
            org=_org(),
            ctx=ctx,
        )

    assert exc.value.status_code == 401
    assert exc.value.detail == "Not authenticated"


@pytest.mark.asyncio
async def test_preview_reflection_promotion_verifies_project_target() -> None:
    org = _org()
    ctx = _ctx()
    http_request = _http_request()
    result = ReflectionPromotionPreview(
        allowed=True,
        candidate_id="candidate-1",
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        raw_source_ids=["source-1"],
        metadata={
            "policy_reasons": [
                "same_scope_reflect_allowed",
                "same_scope_write_allowed",
            ],
            "input_scopes": [
                {
                    "id": "candidate-1",
                    "memory_scope": "private",
                    "scope_key": None,
                }
            ],
            "source_count": 1,
        },
    )
    with (
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()) as verify,
        patch(
            "sibyl.api.routes.memory.preview_reflection_candidate_promotion",
            AsyncMock(return_value=result),
        ) as preview,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await preview_reflection_promotion(
            ReflectionPromotionRequest(
                candidate_id="candidate-1",
                promote_to_scope="project",
                promote_to_scope_key="project_123",
                project="project_123",
                domain="sibyl",
                related_to=["task_123"],
            ),
            http_request=http_request,
            org=org,
            ctx=ctx,
        )

    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    preview.assert_awaited_once_with(
        candidate_id="candidate-1",
        organization_id=str(org.id),
        principal_id="user-123",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
        domain="sibyl",
        project="project_123",
        accessible_projects={"project_123"},
    )
    audit.assert_awaited_once_with(
        action="memory.reflect.promote.preview",
        user_id="user-123",
        organization_id="org-1",
        request=http_request,
        memory_scope="project",
        scope_key="project_123",
        project_id="project_123",
        source_surface="reflection_promote_preview",
        source_ids=["candidate-1", "source-1"],
        derived_ids=[],
        policy_allowed=True,
        policy_reason="promotion_preview_allowed",
        details={
            "domain": "sibyl",
            "preview": True,
            "related_to_count": 1,
            "review_state": "pending",
            "source_count": 1,
        },
    )
    assert response.allowed is True
    assert response.reason == "promotion_preview_allowed"
    assert response.promote_to_scope == "project"
    assert response.promote_to_scope_key == "project_123"
    assert response.raw_source_ids == ["source-1"]
    assert response.policy_reasons == [
        "same_scope_reflect_allowed",
        "same_scope_write_allowed",
    ]
    assert response.input_scopes[0].id == "candidate-1"


@pytest.mark.asyncio
async def test_auto_review_reflection_candidate_promotes_safe_candidate() -> None:
    org = _org()
    ctx = _ctx()
    http_request = _http_request()
    preview = ReflectionPromotionPreview(
        allowed=True,
        candidate_id="candidate-1",
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        raw_source_ids=["source-1"],
        metadata={
            "policy_reasons": [
                "same_scope_reflect_allowed",
                "same_scope_write_allowed",
            ],
            "reflection_confidence": 0.91,
            "source_count": 1,
        },
    )
    promotion = ReflectionPromotionResult(
        success=True,
        candidate_id="candidate-1",
        promoted_id="decision_123",
        reason="promoted",
        review_state="promoted",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        raw_source_ids=["source-1"],
        metadata={
            "policy_reasons": [
                "same_scope_reflect_allowed",
                "same_scope_write_allowed",
            ],
        },
    )
    with (
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()) as verify,
        patch(
            "sibyl.api.routes.memory.preview_reflection_candidate_promotion",
            AsyncMock(return_value=preview),
        ) as preview_call,
        patch(
            "sibyl.api.routes.memory.promote_reflection_candidate_review",
            AsyncMock(return_value=promotion),
        ) as promote,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await auto_review_reflection_candidate(
            ReflectionAutonomyRequest(
                candidate_id="candidate-1",
                promote_to_scope="project",
                promote_to_scope_key="project_123",
                project="project_123",
                domain="sibyl",
                related_to=["task_123"],
            ),
            http_request=http_request,
            org=org,
            ctx=ctx,
        )

    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    preview_call.assert_awaited_once_with(
        candidate_id="candidate-1",
        organization_id=str(org.id),
        principal_id="user-123",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
        domain="sibyl",
        project="project_123",
        accessible_projects={"project_123"},
    )
    promote.assert_awaited_once_with(
        candidate_id="candidate-1",
        organization_id=str(org.id),
        principal_id="user-123",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
        domain="sibyl",
        project="project_123",
        related_to=["task_123"],
        accessible_projects={"project_123"},
    )
    audit.assert_awaited_once_with(
        action="memory.reflect.auto_promote",
        user_id="user-123",
        organization_id="org-1",
        request=http_request,
        memory_scope="project",
        scope_key="project_123",
        project_id="project_123",
        source_surface="reflection_auto_review",
        source_ids=["candidate-1", "source-1"],
        derived_ids=["decision_123"],
        policy_allowed=True,
        policy_reason="auto_promote_candidate",
        details={
            "action_succeeded": True,
            "confidence": 0.91,
            "confidence_threshold": 0.8,
            "domain": "sibyl",
            "dry_run": False,
            "exception_reasons": [],
            "outcome": "auto_promote",
            "recommended_action": "promote",
            "related_to_count": 1,
            "review_state": "promoted",
            "source_count": 1,
        },
    )
    assert response.outcome == "auto_promote"
    assert response.applied is True
    assert response.promoted_id == "decision_123"
    assert response.promotion is not None
    assert response.promotion.promoted_id == "decision_123"


@pytest.mark.asyncio
async def test_auto_review_reflection_candidate_dry_run_does_not_promote() -> None:
    preview = ReflectionPromotionPreview(
        allowed=True,
        candidate_id="candidate-1",
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
        metadata={
            "policy_reasons": [
                "same_scope_reflect_allowed",
                "same_scope_write_allowed",
            ],
            "reflection_confidence": 0.9,
        },
    )
    with (
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.api.routes.memory.preview_reflection_candidate_promotion",
            AsyncMock(return_value=preview),
        ),
        patch(
            "sibyl.api.routes.memory.promote_reflection_candidate_review",
            AsyncMock(),
        ) as promote,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()),
    ):
        response = await auto_review_reflection_candidate(
            ReflectionAutonomyRequest(
                candidate_id="candidate-1",
                promote_to_scope="private",
                dry_run=True,
            ),
            org=_org(),
            ctx=_ctx(),
        )

    promote.assert_not_awaited()
    assert response.outcome == "auto_promote"
    assert response.recommended_action == "promote"
    assert response.applied is False
    assert response.dry_run is True
    assert response.promoted_id is None


@pytest.mark.asyncio
async def test_auto_review_reflection_candidate_routes_exceptions() -> None:
    preview = ReflectionPromotionPreview(
        allowed=False,
        candidate_id="candidate-1",
        reason="unverified_membership",
        review_state="pending",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        raw_source_ids=["source-1"],
        metadata={
            "policy_reasons": ["unverified_membership"],
            "reflection_confidence": 0.9,
        },
    )
    with (
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()),
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_other"}),
        ),
        patch(
            "sibyl.api.routes.memory.preview_reflection_candidate_promotion",
            AsyncMock(return_value=preview),
        ),
        patch(
            "sibyl.api.routes.memory.promote_reflection_candidate_review",
            AsyncMock(),
        ) as promote,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await auto_review_reflection_candidate(
            ReflectionAutonomyRequest(
                candidate_id="candidate-1",
                promote_to_scope="project",
                promote_to_scope_key="project_123",
            ),
            org=_org(),
            ctx=_ctx(),
        )

    promote.assert_not_awaited()
    assert response.outcome == "exception"
    assert response.recommended_action == "route_to_review"
    assert response.applied is False
    assert response.reason == "policy_denied"
    assert response.exception_reasons == ["policy_denied"]
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_reflection_review_dry_run_summarizes_pending_candidates() -> None:
    safe_preview = ReflectionPromotionPreview(
        allowed=True,
        candidate_id="candidate-safe",
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-safe"],
        metadata={"reflection_confidence": 0.92},
    )
    exception_preview = ReflectionPromotionPreview(
        allowed=False,
        candidate_id="candidate-exception",
        reason="unverified_membership",
        review_state="pending",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        raw_source_ids=["source-exception"],
        metadata={
            "policy_reasons": ["unverified_membership"],
            "reflection_confidence": 0.91,
        },
    )
    candidates = [
        RawMemory(
            id="candidate-safe",
            organization_id="org-1",
            source_id="source-safe",
            principal_id="user-123",
            capture_surface="reflection_candidate",
        ),
        RawMemory(
            id="candidate-exception",
            organization_id="org-1",
            source_id="source-exception",
            principal_id="user-123",
            capture_surface="reflection_candidate",
        ),
    ]
    org = _org()
    with (
        patch(
            "sibyl.api.routes.memory.list_reflection_candidate_reviews",
            AsyncMock(return_value=candidates),
        ) as list_candidates,
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ) as access,
        patch(
            "sibyl.api.routes.memory.preview_reflection_candidate_promotion",
            AsyncMock(side_effect=[safe_preview, exception_preview]),
        ),
        patch(
            "sibyl.api.routes.memory.promote_reflection_candidate_review",
            AsyncMock(),
        ) as promote,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()),
    ):
        response = await drain_reflection_review(
            ReflectionReviewDrainRequest(
                dry_run=True,
                limit=2,
                promote_to_scope="private",
            ),
            org=org,
            ctx=_ctx(),
        )

    list_candidates.assert_awaited_once_with(
        organization_id=str(org.id),
        review_state="pending",
        limit=2,
    )
    access.assert_awaited()
    promote.assert_not_awaited()
    assert response.scanned_count == 2
    assert response.auto_promote_count == 1
    assert response.exception_count == 1
    assert response.applied_count == 0
    assert response.results[0].candidate_id == "candidate-safe"
    assert response.results[0].outcome == "auto_promote"
    assert response.results[1].candidate_id == "candidate-exception"
    assert response.results[1].reason == "policy_denied"


@pytest.mark.asyncio
async def test_drain_reflection_review_skips_inaccessible_candidate_scope() -> None:
    inaccessible_candidate = RawMemory(
        id="candidate-secret",
        organization_id="org-1",
        source_id="source-secret",
        principal_id="owner-user",
        memory_scope=MemoryScope.PROJECT,
        scope_key="secret-project",
        metadata={"project_id": "secret-project"},
        capture_surface="reflection_candidate",
    )
    with (
        patch(
            "sibyl.api.routes.memory.list_reflection_candidate_reviews",
            AsyncMock(return_value=[inaccessible_candidate]),
        ),
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"open-project"}),
        ),
        patch(
            "sibyl.api.routes.memory._accessible_projects_for_promotion",
            AsyncMock(return_value={"open-project"}),
        ),
        patch(
            "sibyl.api.routes.memory.preview_reflection_candidate_promotion",
            AsyncMock(),
        ) as preview,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()),
    ):
        response = await drain_reflection_review(
            ReflectionReviewDrainRequest(
                dry_run=False,
                limit=1,
                promote_to_scope="project",
                promote_to_scope_key="open-project",
            ),
            org=_org(),
            ctx=_ctx(),
        )

    preview.assert_not_awaited()
    assert response.scanned_count == 1
    assert response.skip_count == 1
    assert response.results[0].candidate_id == "candidate-secret"
    assert response.results[0].reason == "policy_denied"
    assert response.results[0].policy_reasons == ["unverified_membership"]


@pytest.mark.asyncio
async def test_drain_reflection_review_archives_terminal_exceptions() -> None:
    preview = ReflectionPromotionPreview(
        allowed=True,
        candidate_id="candidate-duplicate",
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
        metadata={
            "candidate_duplicate_of_source_id": "source-0",
            "reflection_confidence": 0.94,
        },
    )
    candidate = RawMemory(
        id="candidate-duplicate",
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-123",
        capture_surface="reflection_candidate",
    )
    archived = RawMemory(
        id="candidate-duplicate",
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-123",
        review_state="archived",
        capture_surface="reflection_candidate",
    )
    with (
        patch(
            "sibyl.api.routes.memory.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.api.routes.memory.preview_reflection_candidate_promotion",
            AsyncMock(return_value=preview),
        ),
        patch(
            "sibyl.api.routes.memory.promote_reflection_candidate_review",
            AsyncMock(),
        ) as promote,
        patch("sibyl.api.routes.memory.get_raw_memory", AsyncMock(return_value=candidate)),
        patch("sibyl.api.routes.memory.save_raw_memory", AsyncMock(return_value=archived)) as save,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await drain_reflection_review(
            ReflectionReviewDrainRequest(
                dry_run=False,
                limit=1,
                promote_to_scope="private",
                archive_exceptions=True,
            ),
            org=_org(),
            ctx=_ctx(),
        )

    promote.assert_not_awaited()
    save.assert_awaited_once()
    assert audit.await_count == 2
    assert response.archived_count == 1
    assert response.results[0].archived is True
    assert response.results[0].review_state == "archived"
    assert response.results[0].exception_reasons == ["duplicate_candidate"]


@pytest.mark.asyncio
async def test_promote_reflection_candidate_verifies_project_target() -> None:
    org = _org()
    ctx = _ctx()
    http_request = _http_request()
    result = ReflectionPromotionResult(
        success=True,
        candidate_id="candidate-1",
        promoted_id="decision_123",
        reason="promoted",
        review_state="promoted",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        raw_source_ids=["source-1"],
        metadata={
            "policy_reasons": [
                "same_scope_reflect_allowed",
                "same_scope_write_allowed",
            ],
        },
    )
    with (
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()) as verify,
        patch(
            "sibyl.api.routes.memory.promote_reflection_candidate_review",
            AsyncMock(return_value=result),
        ) as promote,
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await promote_reflection_candidate(
            ReflectionPromotionRequest(
                candidate_id="candidate-1",
                promote_to_scope="project",
                promote_to_scope_key="project_123",
                project="project_123",
                domain="sibyl",
                related_to=["task_123"],
            ),
            http_request=http_request,
            org=org,
            ctx=ctx,
        )

    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    promote.assert_awaited_once_with(
        candidate_id="candidate-1",
        organization_id=str(org.id),
        principal_id="user-123",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
        domain="sibyl",
        project="project_123",
        related_to=["task_123"],
        accessible_projects={"project_123"},
    )
    audit.assert_awaited_once_with(
        action="memory.reflect.promote",
        user_id="user-123",
        organization_id="org-1",
        request=http_request,
        memory_scope="project",
        scope_key="project_123",
        project_id="project_123",
        source_surface="reflection_promote",
        source_ids=["candidate-1", "source-1"],
        derived_ids=["decision_123"],
        policy_allowed=True,
        policy_reason="promoted",
        details={
            "action_succeeded": True,
            "domain": "sibyl",
            "related_to_count": 1,
            "review_state": "promoted",
        },
    )
    assert response.success is True
    assert response.promoted_id == "decision_123"
    assert response.policy_reasons == [
        "same_scope_reflect_allowed",
        "same_scope_write_allowed",
    ]


@pytest.mark.asyncio
async def test_promote_reflection_candidate_returns_policy_denial() -> None:
    org = _org()
    result = ReflectionPromotionResult(
        success=False,
        candidate_id="candidate-1",
        promoted_id=None,
        reason="missing_promote_to_scope",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
        metadata={"policy_reasons": ["missing_promote_to_scope"]},
    )
    with (
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_123"}),
        ),
        patch(
            "sibyl.api.routes.memory.promote_reflection_candidate_review",
            AsyncMock(return_value=result),
        ),
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
    ):
        response = await promote_reflection_candidate(
            ReflectionPromotionRequest(candidate_id="candidate-1"),
            org=org,
            ctx=_ctx(),
        )

    assert response.success is False
    assert response.reason == "missing_promote_to_scope"
    assert response.policy_reasons == ["missing_promote_to_scope"]
    audit.assert_awaited_once_with(
        action="memory.reflect.promote",
        user_id="user-123",
        organization_id="org-1",
        request=None,
        memory_scope="private",
        scope_key=None,
        project_id=None,
        source_surface="reflection_promote",
        source_ids=["candidate-1", "source-1"],
        derived_ids=[],
        policy_allowed=False,
        policy_reason="missing_promote_to_scope",
        details={
            "action_succeeded": False,
            "domain": None,
            "related_to_count": 0,
            "review_state": "pending",
        },
    )


@pytest.mark.asyncio
async def test_promote_reflection_candidate_returns_404_for_missing_candidate() -> None:
    result = ReflectionPromotionResult(
        success=False,
        candidate_id="missing",
        promoted_id=None,
        reason="candidate_not_found",
        review_state="missing",
        memory_scope=None,
        scope_key=None,
        raw_source_ids=[],
    )
    with (
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.api.routes.memory.promote_reflection_candidate_review",
            AsyncMock(return_value=result),
        ),
        patch("sibyl.api.routes.memory.log_memory_audit_event", AsyncMock()) as audit,
        pytest.raises(HTTPException) as exc,
    ):
        await promote_reflection_candidate(
            ReflectionPromotionRequest(candidate_id="missing"),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 404
    audit.assert_awaited_once_with(
        action="memory.reflect.promote",
        user_id="user-123",
        organization_id="org-1",
        request=None,
        memory_scope=None,
        scope_key=None,
        project_id=None,
        source_surface="reflection_promote",
        source_ids=["missing"],
        derived_ids=[],
        policy_allowed=None,
        policy_reason="candidate_not_found",
        details={
            "action_succeeded": False,
            "domain": None,
            "related_to_count": 0,
            "review_state": "missing",
        },
    )
