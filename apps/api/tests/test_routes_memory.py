from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.memory import (
    list_memory_audit,
    preview_reflection_promotion,
    promote_reflection_candidate,
    recall_raw,
    remember_raw,
)
from sibyl.api.schemas import (
    RawMemoryRecallRequest,
    RawMemoryRememberRequest,
    ReflectionPromotionRequest,
)
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.services.native_memory import (
    NativeReflectionPromotionPreview,
    NativeReflectionPromotionResult,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


def _org() -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    return org


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = "user-123"
    ctx.organization_id = "org-1"
    ctx.org_role = OrganizationRole.MEMBER
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
async def test_remember_raw_denies_project_scope_without_policy_membership() -> None:
    http_request = _http_request()
    with (
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
async def test_preview_reflection_promotion_verifies_project_target() -> None:
    org = _org()
    ctx = _ctx()
    http_request = _http_request()
    result = NativeReflectionPromotionPreview(
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
async def test_promote_reflection_candidate_verifies_project_target() -> None:
    org = _org()
    ctx = _ctx()
    http_request = _http_request()
    result = NativeReflectionPromotionResult(
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
    result = NativeReflectionPromotionResult(
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
    result = NativeReflectionPromotionResult(
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
