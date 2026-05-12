from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.memory import promote_reflection_candidate, recall_raw, remember_raw
from sibyl.api.schemas import (
    RawMemoryRecallRequest,
    RawMemoryRememberRequest,
    ReflectionPromotionRequest,
)
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.services.native_memory import NativeReflectionPromotionResult
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
    with patch(
        "sibyl.api.routes.memory.remember_raw_memory",
        AsyncMock(return_value=_memory(organization_id=str(org.id), source_id="source-1")),
    ) as remember:
        response = await remember_raw(
            RawMemoryRememberRequest(
                title="Raw note",
                raw_content="Sibyl stores raw memory before reflection.",
                source_id="source-1",
                tags=["memory"],
                provenance={"message_id": "msg-1"},
                capture_surface="cli",
            ),
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
    assert response.id == "memory-1"
    assert response.source_id == "source-1"
    assert response.principal_id == "user-123"
    assert response.policy_reason == "same_scope_write_allowed"


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
    with (
        patch(
            "sibyl.api.routes.memory.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_other"}),
        ),
        patch("sibyl.api.routes.memory.log") as route_log,
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
    assert exc.value.detail == "unverified_membership"
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
    with patch(
        "sibyl.api.routes.memory.recall_raw_memory",
        AsyncMock(return_value=[_memory(organization_id=str(org.id))]),
    ) as recall:
        response = await recall_raw(
            RawMemoryRecallRequest(query="raw memory", limit=5),
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
async def test_promote_reflection_candidate_verifies_project_target() -> None:
    org = _org()
    ctx = _ctx()
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
    ):
        response = await promote_reflection_candidate(
            ReflectionPromotionRequest(candidate_id="candidate-1"),
            org=org,
            ctx=_ctx(),
        )

    assert response.success is False
    assert response.reason == "missing_promote_to_scope"
    assert response.policy_reasons == ["missing_promote_to_scope"]


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
        pytest.raises(HTTPException) as exc,
    ):
        await promote_reflection_candidate(
            ReflectionPromotionRequest(candidate_id="missing"),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 404
