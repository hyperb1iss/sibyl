"""Tests for REST ID prefix resolution."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl.api.routes import resolve as resolve_route
from sibyl.auth.context import AuthContext
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


def _auth_context(org: AuthOrganization) -> AuthContext:
    return AuthContext(
        user=AuthUser(id=uuid4(), email="nova@example.test"),
        organization=org,
        org_role=OrganizationRole.MEMBER,
    )


def test_prefix_candidates_adds_typed_prefix() -> None:
    assert resolve_route._prefix_candidates("98efe8", "task") == [
        "98efe8",
        "task_98efe8",
    ]
    assert resolve_route._prefix_candidates("1", "task") == ["1", "task_1"]


@pytest.mark.asyncio
async def test_resolve_id_prefix_filters_inaccessible_project_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    ctx = _auth_context(org)
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_execute(group_id: str, query: str, **params: object) -> list[dict[str, object]]:
        calls.append((group_id, params))
        return [
            {
                "uuid": "task_visible123456",
                "entity_type": "task",
                "name": "Visible task",
                "project_id": "project-visible",
                "metadata": {},
            },
            {
                "uuid": "task_hidden123456",
                "entity_type": "task",
                "name": "Hidden task",
                "project_id": "project-hidden",
                "metadata": {},
            },
        ]

    async def fake_accessible(_: AuthContext) -> set[str]:
        return {"project-visible"}

    monkeypatch.setattr(resolve_route, "execute_surreal_graph_query", fake_execute)
    monkeypatch.setattr(resolve_route, "list_accessible_project_graph_ids", fake_accessible)

    response = await resolve_route.resolve_id_prefix(
        "task_",
        org=org,
        ctx=ctx,
        entity_type="task",
        limit=20,
    )

    assert response.count == 1
    assert [match.id for match in response.matches] == ["task_visible123456"]
    assert calls[0][0] == str(org.id)
    assert calls[0][1]["entity_type"] == "task"


@pytest.mark.asyncio
async def test_resolve_raw_memory_prefix_filters_policy_denied_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    ctx = _auth_context(org)
    memories = [
        RawMemory(
            id="memory-visible",
            organization_id=str(org.id),
            source_id="source-visible",
            principal_id=ctx.user_id or "",
            memory_scope=MemoryScope.PROJECT,
            scope_key="project-visible",
            project_id="project-visible",
            title="Visible memory",
        ),
        RawMemory(
            id="memory-hidden",
            organization_id=str(org.id),
            source_id="source-hidden",
            principal_id="other-user",
            memory_scope=MemoryScope.PROJECT,
            scope_key="project-hidden",
            project_id="project-hidden",
            title="Hidden memory",
        ),
    ]

    async def fake_resolve_raw(
        *,
        organization_id: str,
        prefix: str,
        limit: int,
    ) -> list[RawMemory]:
        assert organization_id == str(org.id)
        assert prefix == "memory"
        assert limit == 20
        return memories

    async def fake_policy(*, ctx: AuthContext, memory: RawMemory) -> SimpleNamespace:
        return SimpleNamespace(allowed=memory.id == "memory-visible")

    monkeypatch.setattr(resolve_route, "resolve_raw_memory_prefix", fake_resolve_raw)
    monkeypatch.setattr(resolve_route, "_inspect_content_policy", fake_policy)

    response = await resolve_route.resolve_id_prefix(
        "memory",
        org=org,
        ctx=ctx,
        entity_type="raw_memory",
        limit=20,
    )

    assert response.count == 1
    assert response.matches[0].id == "memory-visible"
    assert response.matches[0].entity_type == "raw_memory"


@pytest.mark.asyncio
async def test_resolve_raw_memory_prefix_filters_api_key_memory_scope_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    ctx = _auth_context(org)
    ctx.api_key_memory_scope_keys = {"project\x1fproject-visible"}
    memories = [
        RawMemory(
            id="memory-visible",
            organization_id=str(org.id),
            source_id="source-visible",
            principal_id=ctx.user_id or "",
            memory_scope=MemoryScope.PROJECT,
            scope_key="project-visible",
            project_id="project-visible",
            title="Visible memory",
        ),
        RawMemory(
            id="memory-hidden",
            organization_id=str(org.id),
            source_id="source-hidden",
            principal_id="other-user",
            memory_scope=MemoryScope.PROJECT,
            scope_key="project-hidden",
            project_id="project-hidden",
            title="Hidden memory",
        ),
    ]

    async def fake_resolve_raw(
        *,
        organization_id: str,
        prefix: str,
        limit: int,
    ) -> list[RawMemory]:
        assert organization_id == str(org.id)
        assert prefix == "memory"
        assert limit == 20
        return memories

    async def fake_policy(*, ctx: AuthContext, memory: RawMemory) -> SimpleNamespace:
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(resolve_route, "resolve_raw_memory_prefix", fake_resolve_raw)
    monkeypatch.setattr(resolve_route, "_inspect_content_policy", fake_policy)

    response = await resolve_route.resolve_id_prefix(
        "memory",
        org=org,
        ctx=ctx,
        entity_type="raw_memory",
        limit=20,
    )

    assert response.count == 1
    assert response.matches[0].id == "memory-visible"
    assert response.matches[0].entity_type == "raw_memory"
