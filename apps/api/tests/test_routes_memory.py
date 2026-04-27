from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.memory import recall_raw, remember_raw
from sibyl.api.schemas import RawMemoryRecallRequest, RawMemoryRememberRequest
from sibyl.db.models import OrganizationRole, ProjectRole
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


def _org() -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    return org


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = "user-123"
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


@pytest.mark.asyncio
async def test_remember_raw_diary_sets_agent_metadata_and_surface() -> None:
    org = _org()
    ctx = _ctx()
    with (
        patch("sibyl.api.routes.memory.get_project_record_by_graph_id", AsyncMock()) as get_project,
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

    get_project.assert_awaited_once_with(
        organization_id=org.id,
        graph_project_id="project_123",
    )
    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
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
async def test_remember_raw_verifies_project_scope_write_access() -> None:
    org = _org()
    ctx = _ctx()
    with (
        patch("sibyl.api.routes.memory.get_project_record_by_graph_id", AsyncMock()) as get_project,
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()) as verify,
        patch(
            "sibyl.api.routes.memory.remember_raw_memory",
            AsyncMock(return_value=_memory(organization_id=str(org.id), scope_key="project_123")),
        ),
    ):
        await remember_raw(
            RawMemoryRememberRequest(
                raw_content="project note",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=org,
            ctx=ctx,
        )

    get_project.assert_awaited_once_with(
        organization_id=org.id,
        graph_project_id="project_123",
    )
    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.CONTRIBUTOR,
    )


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
    assert [memory.id for memory in response.memories] == ["memory-1"]


@pytest.mark.asyncio
async def test_recall_raw_diary_filters_agent_and_project() -> None:
    org = _org()
    ctx = _ctx()
    with (
        patch("sibyl.api.routes.memory.get_project_record_by_graph_id", AsyncMock()) as get_project,
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()) as verify,
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock(return_value=[])) as recall,
    ):
        await recall_raw(
            RawMemoryRecallRequest(
                query="implementation state",
                diary=True,
                agent_id="nova",
                project_id="project_123",
            ),
            org=org,
            ctx=ctx,
        )

    get_project.assert_awaited_once_with(
        organization_id=org.id,
        graph_project_id="project_123",
    )
    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.VIEWER,
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
async def test_recall_raw_verifies_project_scope_read_access() -> None:
    org = _org()
    ctx = _ctx()
    with (
        patch("sibyl.api.routes.memory.get_project_record_by_graph_id", AsyncMock()) as get_project,
        patch("sibyl.api.routes.memory.verify_entity_project_access", AsyncMock()) as verify,
        patch("sibyl.api.routes.memory.recall_raw_memory", AsyncMock(return_value=[])),
    ):
        await recall_raw(
            RawMemoryRecallRequest(
                query="project memory",
                memory_scope="project",
                scope_key="project_123",
            ),
            org=org,
            ctx=ctx,
        )

    get_project.assert_awaited_once_with(
        organization_id=org.id,
        graph_project_id="project_123",
    )
    verify.assert_awaited_once_with(
        None,
        ctx,
        "project_123",
        required_role=ProjectRole.VIEWER,
    )


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
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_raw_maps_scope_errors_to_400() -> None:
    with (
        patch(
            "sibyl.api.routes.memory.recall_raw_memory",
            AsyncMock(side_effect=ValueError("project raw memory requires a scope_key")),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await recall_raw(
            RawMemoryRecallRequest(query="raw memory", memory_scope="project", limit=5),
            org=_org(),
            ctx=_ctx(),
        )

    assert exc.value.status_code == 400
