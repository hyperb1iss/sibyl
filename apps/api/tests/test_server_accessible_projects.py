from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.server import (
    McpContext,
    _authorize_mcp_memory_write,
    _compile_mcp_context_pack,
    _get_accessible_projects,
    _get_mcp_context,
    _reflect_mcp_memory,
    _remember_mcp_memory,
    _require_owner_mcp_context,
    _resolve_mcp_capture_links,
    _resolve_mcp_project_scope,
)
from sibyl_core.auth import AuthOrganization, AuthUser
from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextLayer,
    ContextPack,
    ContextSection,
)
from sibyl_core.models.reflection import ReflectionCandidate, ReflectionPack


def _context_pack() -> ContextPack:
    return ContextPack(
        goal="ship faster",
        intent=ContextIntent.BUILD,
        query="ship faster",
        domain="sibyl",
        project="project-a",
        sections=[
            ContextSection(
                facet=ContextFacet.DECISIONS,
                title="Decisions",
                items=[
                    ContextItem(
                        id="decision_1",
                        type="decision",
                        name="Use context packs",
                        content="Agents should receive precise grouped memory.",
                        score=0.91,
                        facet=ContextFacet.DECISIONS,
                        reason="decision records a choice",
                        source="Northstar",
                    )
                ],
            )
        ],
        total_items=1,
        layer=ContextLayer.WAKE,
    )


@pytest.mark.asyncio
async def test_accessible_projects_intersects_with_api_key_scope() -> None:
    user = AuthUser(id=uuid4(), email="nova@example.com", name="Nova")
    organization = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    ctx = McpContext(
        org_id=str(organization.id),
        user_id=str(user.id),
        scopes=["api:read"],
        api_key_project_ids=["project-a", "project-b"],
    )
    resolve_projects = AsyncMock(return_value={"project-b"})

    with patch("sibyl.server.resolve_accessible_project_graph_ids", resolve_projects):
        result = await _get_accessible_projects(ctx)

    assert result == {"project-b"}
    resolve_projects.assert_awaited_once_with(
        user_id=ctx.user_id,
        org_id=ctx.org_id,
        scopes=ctx.scopes,
        api_key_project_ids=ctx.api_key_project_ids,
    )


@pytest.mark.asyncio
async def test_accessible_projects_returns_empty_when_user_disappears() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["api:read"])
    with patch(
        "sibyl.server.resolve_accessible_project_graph_ids",
        AsyncMock(return_value=set()),
    ):
        result = await _get_accessible_projects(ctx)

    assert result == set()


@pytest.mark.asyncio
async def test_resolve_mcp_project_scope_filters_unscoped_reads() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    with patch("sibyl.server._get_accessible_projects", AsyncMock(return_value={"project-a"})):
        result = await _resolve_mcp_project_scope(ctx, project=None)

    assert result == {"project-a"}


@pytest.mark.asyncio
async def test_resolve_mcp_project_scope_allows_explicit_accessible_project() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    with patch("sibyl.server._get_accessible_projects", AsyncMock(return_value={"project-a"})):
        result = await _resolve_mcp_project_scope(ctx, project="project-a")

    assert result == {"project-a"}


@pytest.mark.asyncio
async def test_resolve_mcp_project_scope_returns_explicit_project_for_unfiltered_context() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    with patch("sibyl.server._get_accessible_projects", AsyncMock(return_value=None)):
        result = await _resolve_mcp_project_scope(ctx, project="project-a")

    assert result == {"project-a"}


@pytest.mark.asyncio
async def test_resolve_mcp_project_scope_rejects_inaccessible_project() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    with (
        patch("sibyl.server._get_accessible_projects", AsyncMock(return_value={"project-a"})),
        pytest.raises(ValueError, match="Project access denied: project-b"),
    ):
        await _resolve_mcp_project_scope(ctx, project="project-b")


@pytest.mark.asyncio
async def test_resolve_mcp_project_scope_requires_project_for_restricted_writes() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    with (
        patch("sibyl.server._get_accessible_projects", AsyncMock(return_value={"project-a"})),
        pytest.raises(ValueError, match="Project is required"),
    ):
        await _resolve_mcp_project_scope(
            ctx,
            project=None,
            require_project_when_restricted=True,
        )


@pytest.mark.asyncio
async def test_compile_mcp_context_pack_audits_render_receipt() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])
    compile_context = AsyncMock(return_value=_context_pack())

    with (
        patch("sibyl.server._require_mcp_context", AsyncMock(return_value=ctx)),
        patch(
            "sibyl.server._resolve_mcp_project_scope",
            AsyncMock(return_value={"project-a"}),
        ) as resolve_scope,
        patch("sibyl_core.tools.core.compile_context", compile_context),
        patch("sibyl.api.context_audit.log_memory_audit_event", AsyncMock()) as audit,
    ):
        result = await _compile_mcp_context_pack(
            goal="ship faster",
            intent="build",
            layer="wake",
            domain="sibyl",
            project="project-a",
            agent_id="nova",
            limit=8,
            include_related=False,
            related_limit=0,
        )

    assert result["layer"] == ContextLayer.WAKE
    assert result["markdown"].startswith("# Sibyl Context Pack")
    resolve_scope.assert_awaited_once_with(ctx, "project-a")
    compile_context.assert_awaited_once()
    assert compile_context.await_args.kwargs["accessible_projects"] == {"project-a"}
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "memory.context_pack"
    assert kwargs["user_id"] == ctx.user_id
    assert kwargs["organization_id"] == ctx.org_id
    assert kwargs["memory_scope"] == "project"
    assert kwargs["scope_key"] == "project-a"
    assert kwargs["project_id"] == "project-a"
    assert kwargs["source_surface"] == "mcp_context"
    assert kwargs["source_ids"] == ["Northstar"]
    assert kwargs["derived_ids"] == ["decision_1"]
    assert kwargs["details"]["agent_id"] == "nova"
    assert kwargs["details"]["domain"] == "sibyl"
    assert kwargs["details"]["layer"] == "wake"
    assert kwargs["details"]["accessible_project_count"] == 1


@pytest.mark.asyncio
async def test_remember_mcp_memory_scopes_project_metadata() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])
    add = AsyncMock(return_value={"success": True, "id": "decision_123"})
    remember_raw = AsyncMock(
        return_value=SimpleNamespace(id="raw_123", source_id="mcp:remember:decision")
    )

    with (
        patch("sibyl.server._require_mcp_context", AsyncMock(return_value=ctx)),
        patch("sibyl.server._get_accessible_projects", AsyncMock(return_value={"project-a"})),
        patch("sibyl_core.tools.core.add", add),
        patch("sibyl_core.services.surreal_content.remember_raw_memory", remember_raw),
        patch(
            "sibyl_core.tools.core.explore",
            AsyncMock(return_value=SimpleNamespace(entities=[])),
        ),
    ):
        result = await _remember_mcp_memory(
            title="Use scoped memory",
            content="Remember writes should attach to the target project.",
            kind="decision",
            domain="sibyl",
            project="project-a",
            tags=["memory"],
            related_to=["plan_1"],
            metadata={"source": "test"},
        )

    assert result == {
        "success": True,
        "id": "decision_123",
        "raw_memory_id": "raw_123",
        "raw_source_id": "mcp:remember:decision",
        "policy_reason": "same_scope_write_allowed",
    }
    remember_raw.assert_awaited_once_with(
        organization_id=ctx.org_id,
        principal_id=ctx.user_id,
        source_id="mcp:remember:decision",
        raw_content="Remember writes should attach to the target project.",
        title="Use scoped memory",
        memory_scope="project",
        scope_key="project-a",
        tags=["memory"],
        metadata={
            "source": "test",
            "capture_kind": "decision",
            "organization_id": ctx.org_id,
            "domain": "sibyl",
            "project_id": "project-a",
            "created_by": ctx.user_id,
        },
        provenance={"remember_kind": "decision", "related_to": ["plan_1"]},
        capture_surface="mcp",
    )
    add.assert_awaited_once_with(
        title="Use scoped memory",
        content="Remember writes should attach to the target project.",
        entity_type="decision",
        category="sibyl",
        tags=["memory"],
        related_to=["plan_1"],
        metadata={
            "source": "test",
            "capture_kind": "decision",
            "organization_id": ctx.org_id,
            "domain": "sibyl",
            "project_id": "project-a",
            "created_by": ctx.user_id,
            "raw_memory_id": "raw_123",
            "raw_source_id": "mcp:remember:decision",
        },
        project="project-a",
    )


def test_authorize_mcp_memory_write_returns_policy_reason() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    decision = _authorize_mcp_memory_write(
        ctx=ctx,
        memory_scope="project",
        scope_key="project-a",
        accessible_projects={"project-a"},
        surface="mcp_remember",
    )

    assert decision.allowed
    assert decision.reason == "same_scope_write_allowed"
    assert decision.policy_context is not None
    assert decision.policy_context.actor_user_id == ctx.user_id
    assert decision.policy_context.organization_id == ctx.org_id
    assert decision.policy_context.accessible_projects == frozenset({"project-a"})
    assert decision.policy_context.memory_space == "project"
    assert decision.policy_context.scope_key == "project-a"
    assert decision.policy_context.source_surface == "mcp_remember"


def test_authorize_mcp_memory_write_denies_unverified_project() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    with pytest.raises(ValueError, match="unverified_membership"):
        _authorize_mcp_memory_write(
            ctx=ctx,
            memory_scope="project",
            scope_key="project-b",
            accessible_projects={"project-a"},
            surface="mcp_remember",
        )


@pytest.mark.asyncio
async def test_remember_mcp_memory_links_single_active_project_task() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])
    add = AsyncMock(return_value={"success": True, "id": "decision_123"})
    remember_raw = AsyncMock(
        return_value=SimpleNamespace(id="raw_123", source_id="mcp:remember:decision")
    )
    explore = AsyncMock(return_value=SimpleNamespace(entities=[SimpleNamespace(id="task_active")]))

    with (
        patch("sibyl.server._require_mcp_context", AsyncMock(return_value=ctx)),
        patch("sibyl.server._get_accessible_projects", AsyncMock(return_value={"project-a"})),
        patch("sibyl_core.tools.core.add", add),
        patch("sibyl_core.services.surreal_content.remember_raw_memory", remember_raw),
        patch("sibyl_core.tools.core.explore", explore),
    ):
        await _remember_mcp_memory(
            title="Use scoped memory",
            content="Remember writes should attach to the active task.",
            kind="decision",
            domain="sibyl",
            project="project-a",
            tags=None,
            related_to=["plan_1"],
            task_ids=["task_manual", "plan_1"],
            metadata=None,
        )

    add.assert_awaited_once()
    assert add.await_args.kwargs["related_to"] == ["plan_1", "task_manual", "task_active"]
    assert add.await_args.kwargs["metadata"]["raw_memory_id"] == "raw_123"
    assert remember_raw.await_args.kwargs["provenance"]["related_to"] == [
        "plan_1",
        "task_manual",
        "task_active",
    ]
    explore.assert_awaited_once_with(
        mode="list",
        types=["task"],
        project="project-a",
        status="doing",
        limit=2,
        organization_id=ctx.org_id,
    )


@pytest.mark.asyncio
async def test_resolve_mcp_capture_links_skips_active_lookup_without_project() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    with patch("sibyl_core.tools.core.explore", AsyncMock()) as explore:
        links = await _resolve_mcp_capture_links(
            ctx=ctx,
            project=None,
            related_to=["plan_1"],
            task_ids=["task_1", "plan_1"],
            active_task=True,
        )

    assert links == ["plan_1", "task_1"]
    explore.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_mcp_capture_links_skips_ambiguous_active_tasks() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])
    explore = AsyncMock(
        return_value=SimpleNamespace(
            entities=[SimpleNamespace(id="task_one"), SimpleNamespace(id="task_two")]
        )
    )

    with patch("sibyl_core.tools.core.explore", explore):
        links = await _resolve_mcp_capture_links(
            ctx=ctx,
            project="project-a",
            related_to=["plan_1"],
            task_ids=None,
            active_task=True,
        )

    assert links == ["plan_1"]


@pytest.mark.asyncio
async def test_resolve_mcp_capture_links_falls_back_when_lookup_fails() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])

    with patch("sibyl_core.tools.core.explore", AsyncMock(side_effect=RuntimeError("boom"))):
        links = await _resolve_mcp_capture_links(
            ctx=ctx,
            project="project-a",
            related_to=["plan_1"],
            task_ids=["task_1"],
            active_task=True,
        )

    assert links == ["plan_1", "task_1"]


@pytest.mark.asyncio
async def test_reflect_mcp_memory_links_single_active_task_when_persisting() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=["mcp"])
    pack = ReflectionPack(
        source_title="Planning",
        source_id="session_1",
        intent="build",
        domain="sibyl",
        project="project-a",
        candidates=[
            ReflectionCandidate(
                kind="decision",
                title="Decision: Use reflection",
                content="Reflect writes should attach to task context.",
                reason="captures a choice",
                confidence=0.87,
            )
        ],
        total_candidates=1,
    )
    reflect_memory = AsyncMock(return_value=pack)
    explore = AsyncMock(return_value=SimpleNamespace(entities=[SimpleNamespace(id="task_active")]))

    with (
        patch("sibyl.server._require_mcp_context", AsyncMock(return_value=ctx)),
        patch("sibyl.server._get_accessible_projects", AsyncMock(return_value={"project-a"})),
        patch("sibyl_core.tools.core.reflect_memory", reflect_memory),
        patch("sibyl_core.tools.core.explore", explore),
        patch("sibyl.api.context_audit.log_memory_audit_event", AsyncMock()) as audit,
    ):
        result = await _reflect_mcp_memory(
            content="We decided to link reflection to active work.",
            source_title="Planning",
            intent="build",
            domain="sibyl",
            project="project-a",
            related_to=["plan_1"],
            task_ids=["task_manual", "plan_1"],
            persist=True,
        )

    assert result["source_title"] == "Planning"
    assert reflect_memory.await_args.kwargs["related_to"] == [
        "plan_1",
        "task_manual",
        "task_active",
    ]
    assert reflect_memory.await_args.kwargs["principal_id"] == ctx.user_id
    assert reflect_memory.await_args.kwargs["accessible_projects"] == {"project-a"}
    assert reflect_memory.await_args.kwargs["memory_scope"] == "project"
    assert reflect_memory.await_args.kwargs["scope_key"] == "project-a"
    assert reflect_memory.await_args.kwargs["persist_review"] is False
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "memory.reflect"
    assert audit.await_args.kwargs["source_surface"] == "mcp_reflect"
    assert audit.await_args.kwargs["memory_scope"] == "project"
    assert audit.await_args.kwargs["scope_key"] == "project-a"
    assert audit.await_args.kwargs["source_ids"] == ["session_1"]
    assert audit.await_args.kwargs["derived_ids"] == []
    assert audit.await_args.kwargs["details"]["related_to_count"] == 3
    explore.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_mcp_context_uses_legacy_api_key_auth() -> None:
    raw = "sk_live_test_token"
    auth = SimpleNamespace(
        organization_id=uuid4(),
        user_id=uuid4(),
        scopes=["mcp"],
        project_ids=[uuid4()],
    )

    with (
        patch("sibyl.server.get_access_token", return_value=SimpleNamespace(token=raw)),
        patch("sibyl.server.authenticate_api_key", AsyncMock(return_value=auth)) as authenticate,
    ):
        result = await _get_mcp_context()

    assert result == McpContext(
        org_id=str(auth.organization_id),
        user_id=str(auth.user_id),
        scopes=["mcp"],
        api_key_project_ids=[str(auth.project_ids[0])],
    )
    authenticate.assert_awaited_once_with(raw)


@pytest.mark.asyncio
async def test_require_owner_mcp_context_uses_legacy_owner_check() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()))

    with patch(
        "sibyl.server.has_owner_membership",
        AsyncMock(return_value=True),
    ) as has_owner:
        await _require_owner_mcp_context(ctx)

    has_owner.assert_awaited_once_with(org_id=ctx.org_id, user_id=ctx.user_id)


@pytest.mark.asyncio
async def test_require_owner_mcp_context_rejects_non_owner() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()))

    with (
        patch(
            "sibyl.server.has_owner_membership",
            AsyncMock(return_value=False),
        ),
        pytest.raises(ValueError, match="OWNER role required for log access"),
    ):
        await _require_owner_mcp_context(ctx)
