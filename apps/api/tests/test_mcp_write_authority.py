"""MCP write authorization resolves roles independently of readable evidence."""

from unittest.mock import AsyncMock

import pytest

from sibyl.mcp_tools import context, management
from sibyl.mcp_tools.context import McpContext
from sibyl.mcp_tools.synthesis import _synthesis_mcp_draft


@pytest.mark.parametrize("can_write", [False, True])
async def test_synthesis_requires_contributor_and_preserves_readable_evidence(
    monkeypatch, can_write
):
    ctx = McpContext(org_id="org-test", user_id="user-test", scopes=["mcp"])
    readable = {"project-a", "reference-project"}

    resolver = AsyncMock(return_value=(readable, {"project-a"} if can_write else set()))
    draft = AsyncMock(return_value={"artifact": {}})
    monkeypatch.setattr(context, "require_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(context, "resolve_project_graph_grants", resolver)
    monkeypatch.setattr("sibyl_core.tools.core.synthesis_draft", draft)
    if can_write:
        await _synthesis_mcp_draft(
            goal="Summarize project decisions",
            project=None,
            scope_key="project-a",
            remember=True,
            memory_scope="project",
        )
        assert draft.await_args.kwargs["accessible_projects"] == readable
    else:
        with pytest.raises(
            ValueError, match="unverified_membership: project writes require contributor access"
        ):
            await _synthesis_mcp_draft(
                goal="Summarize project decisions",
                project=None,
                scope_key="project-a",
                remember=True,
                memory_scope="project",
            )
        draft.assert_not_awaited()
    resolver.assert_awaited_once()


@pytest.mark.parametrize("action", ["estimate", "suggest", "prioritize", "detect_cycles"])
@pytest.mark.parametrize("org_role", ["member", "viewer"])
@pytest.mark.parametrize("is_api_key", [False, True])
async def test_manage_analysis_remains_available_to_project_readers(
    monkeypatch, action, org_role, is_api_key
):
    ctx = McpContext(
        org_id="org-test",
        user_id="user-test",
        scopes=["mcp", "api:read"],
        org_role=org_role,
        is_api_key=is_api_key,
    )
    require_context = AsyncMock(wraps=context.require_context)
    monkeypatch.setattr(context, "get_context", AsyncMock(return_value=ctx))
    writes = AsyncMock(return_value=set())
    manage = AsyncMock(return_value={"success": True, "action": action, "data": {}})
    monkeypatch.setattr(context, "require_context", require_context)
    monkeypatch.setattr(context, "get_accessible_projects", AsyncMock(return_value={"project-a"}))
    monkeypatch.setattr(context, "get_writable_projects", writes)
    monkeypatch.setattr(
        management,
        "_mcp_entity_write_target",
        AsyncMock(return_value=("project-a", {"project_id": "project-a"})),
    )
    monkeypatch.setattr("sibyl_core.tools.manage.manage", manage)
    result = await management._manage_mcp_action(
        action=action,
        entity_id="project-a" if action in {"prioritize", "detect_cycles"} else "task-a",
        data=None,
    )
    assert result["success"]
    require_context.assert_awaited_once_with(write=False)
    writes.assert_not_awaited()
    manage.assert_awaited_once()
    assert manage.await_args.kwargs["accessible_projects"] == {"project-a"}


async def test_project_grants_share_one_resolution_and_return_independent_sets(monkeypatch):
    import asyncio

    resolver = AsyncMock(return_value=(frozenset({"reference", "target"}), frozenset({"target"})))
    monkeypatch.setattr(context, "resolve_project_graph_grants", resolver)
    ctx = McpContext(org_id="org", user_id="user", scopes=["mcp"], api_key_project_ids=["target"])
    readable, writable = await asyncio.gather(
        context.get_accessible_projects(ctx), context.get_writable_projects(ctx)
    )
    resolver.assert_awaited_once_with(
        user_id="user", org_id="org", scopes=["mcp"], api_key_project_ids=["target"]
    )
    assert readable == {"reference", "target"}
    assert writable == {"target"}
    writable.clear()
    assert await context.get_writable_projects(ctx) == {"target"}
    other_request = McpContext(org_id="org", user_id="user", scopes=["mcp"])
    await context.get_accessible_projects(other_request)
    assert resolver.await_count == 2


async def test_writable_projects_without_principal_never_resolves_grants(monkeypatch):
    resolver = AsyncMock()
    monkeypatch.setattr(context, "resolve_project_graph_grants", resolver)
    assert await context.get_writable_projects(McpContext(org_id="org")) == set()
    resolver.assert_not_awaited()


@pytest.mark.parametrize("action", ["estimate", "suggest", "prioritize", "detect_cycles"])
@pytest.mark.parametrize("is_api_key", [False, True])
async def test_keyed_analysis_requires_write_before_reserving_or_reading(
    monkeypatch, action, is_api_key
):
    ctx = McpContext(
        org_id="org-test",
        user_id="user-test",
        scopes=["mcp", "api:read"],
        org_role="member" if is_api_key else "viewer",
        is_api_key=is_api_key,
    )
    monkeypatch.setattr(context, "get_context", AsyncMock(return_value=ctx))
    require_context = AsyncMock(wraps=context.require_context)
    monkeypatch.setattr(context, "require_context", require_context)
    projects = AsyncMock()
    reserve = AsyncMock()
    manage = AsyncMock()
    lock = AsyncMock()
    monkeypatch.setattr(context, "get_accessible_projects", projects)
    monkeypatch.setattr(management, "reserve_idempotency_record", reserve)
    monkeypatch.setattr("sibyl_core.tools.manage.manage", manage)
    monkeypatch.setattr("sibyl.mcp_tools.idempotency.idempotency_lock", lock)
    reason = "missing the scope required" if is_api_key else "organization_write_forbidden"
    with pytest.raises(ValueError, match=reason):
        await management._manage_mcp_action(
            action=action,
            entity_id="project-a",
            data={"idempotency_key": " request-one "},
        )
    require_context.assert_awaited_once_with(write=True)
    projects.assert_not_awaited()
    reserve.assert_not_awaited()
    manage.assert_not_awaited()
    lock.assert_not_called()
