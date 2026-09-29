"""Public retrieval scopes narrow membership without granting new authority."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from sibyl.api.routes.search import search
from sibyl.api.schemas import SearchRequest
from sibyl.mcp_tools.context import McpContext
from sibyl.mcp_tools.retrieval import register_retrieval_tools
from sibyl_core.auth import OrganizationRole
from tests.harness.auth import stub_auth_context
from tests.test_routes_search import _SearchResult


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, ""])
async def test_mcp_search_never_widens_an_unselected_project(project):
    registry = ToolRegistry()
    register_retrieval_tools(registry)
    ctx = McpContext(org_id=str(uuid4()), user_id="owner")
    with (
        patch("sibyl.mcp_tools.context.require_context", AsyncMock(return_value=ctx)),
        patch("sibyl_core.tools.core.search", AsyncMock()) as core,
        pytest.raises(ValueError, match="all_projects=True"),
    ):
        await registry.tools["search"]("telescope", project=project)
    core.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_search_authentication_precedes_scope_validation():
    registry = ToolRegistry()
    register_retrieval_tools(registry)
    with (
        patch("sibyl.mcp_tools.context.require_context", AsyncMock(side_effect=ValueError("auth"))),
        pytest.raises(ValueError, match=r"^auth$"),
    ):
        await registry.tools["search"]("telescope")


@pytest.mark.asyncio
@pytest.mark.parametrize("all_projects", [False, True])
async def test_mcp_search_preserves_principal_grants_and_labels_widening(all_projects):
    registry = ToolRegistry()
    register_retrieval_tools(registry)
    ctx = McpContext(
        org_id=str(uuid4()), user_id="owner", api_key_memory_scope_keys=["private:owner"]
    )
    with (
        patch("sibyl.mcp_tools.context.require_context", AsyncMock(return_value=ctx)),
        patch(
            "sibyl.mcp_tools.context.get_accessible_projects", AsyncMock(return_value={"a", "b"})
        ),
        patch(
            "sibyl_core.tools.core.search",
            AsyncMock(return_value=_SearchResult([], 0, "telescope")),
        ) as core,
    ):
        result = await registry.tools["search"](
            "telescope", project=None if all_projects else "a", all_projects=all_projects
        )
    assert core.await_args.kwargs["accessible_projects"] == ({"a", "b"} if all_projects else {"a"})
    assert core.await_args.kwargs["principal_id"] == "owner"
    assert core.await_args.kwargs["allowed_memory_scope_keys"] == {"private:owner"}
    assert result["filters"]["scope"] == ("all_projects" if all_projects else "project")


@pytest.mark.asyncio
async def test_mcp_search_rejects_an_inaccessible_project_before_retrieval():
    registry = ToolRegistry()
    register_retrieval_tools(registry)
    with (
        patch(
            "sibyl.mcp_tools.context.require_context",
            AsyncMock(return_value=McpContext(org_id="org", user_id="owner")),
        ),
        patch("sibyl.mcp_tools.context.get_accessible_projects", AsyncMock(return_value={"a"})),
        patch("sibyl_core.tools.core.search", AsyncMock()) as core,
        pytest.raises(ValueError, match="Project access denied"),
    ):
        await registry.tools["search"]("telescope", project="b")
    core.assert_not_awaited()


@pytest.mark.asyncio
async def test_rest_search_verifies_every_selected_project_and_threads_selection():
    ctx = stub_auth_context()
    with (
        patch("sibyl.api.routes.search.verify_entity_project_access", AsyncMock()) as verify,
        patch(
            "sibyl.api.routes.search.list_accessible_project_graph_ids",
            AsyncMock(return_value={"a", "b", "c"}),
        ),
        patch(
            "sibyl_core.tools.core.search",
            AsyncMock(return_value=_SearchResult([], 0, "telescope")),
        ) as core,
        patch("sibyl.api.routes.search.configured_embedding_provider", return_value=object()),
        patch("sibyl.api.routes.search.capture_embedding_usage", return_value=nullcontext({})),
    ):
        result = await search(
            SearchRequest(query="telescope", project_ids=["a", "b"]),
            org=SimpleNamespace(id=uuid4()),
            ctx=ctx,
        )
    assert [call.args[2] for call in verify.await_args_list] == ["a", "b"]
    assert all(call.kwargs["require_existing_project"] for call in verify.await_args_list)
    assert core.await_args.kwargs["accessible_projects"] == {"a", "b"}
    assert core.await_args.kwargs["project_ids"] == ["a", "b"]
    assert core.await_args.kwargs["principal_id"] == ctx.user_id
    assert result.filters["scope"] == "project_selection"


@pytest.mark.asyncio
async def test_rest_search_refuses_foreign_project_in_selection():
    with (
        patch(
            "sibyl.api.routes.search.verify_entity_project_access",
            AsyncMock(side_effect=HTTPException(403, "denied")),
        ),
        patch("sibyl_core.tools.core.search", AsyncMock()) as core,
        pytest.raises(HTTPException),
    ):
        await search(
            SearchRequest(query="telescope", project_ids=["foreign"]),
            org=SimpleNamespace(id=uuid4()),
            ctx=stub_auth_context(),
        )
    core.assert_not_awaited()


def test_rest_search_rejects_empty_selection():
    with pytest.raises(ValidationError):
        SearchRequest(query="telescope", project_ids=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "grants"),
    [(["b"], {"a"}), (["a", "b"], {"a"}), (["a"], set()), (["b"], set()), (["a", "b"], set())],
)
async def test_rest_selection_cannot_exceed_owner_api_key_project_grants(selection, grants):
    ctx = stub_auth_context(
        org_role=OrganizationRole.OWNER,
        api_key_project_ids=frozenset(grants),
    )
    with (
        patch("sibyl.api.routes.search.verify_entity_project_access", AsyncMock()) as verify,
        patch(
            "sibyl.api.routes.search.list_accessible_project_graph_ids",
            AsyncMock(return_value=grants),
        ),
        patch("sibyl_core.tools.core.search", AsyncMock()) as core,
        pytest.raises(HTTPException) as denied,
    ):
        await search(
            SearchRequest(query="telescope", project_ids=selection),
            org=SimpleNamespace(id=uuid4()),
            ctx=ctx,
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "project_scope_denied"
    verify.assert_not_awaited()
    core.assert_not_awaited()


@pytest.mark.asyncio
async def test_rest_single_project_cannot_exceed_owner_api_key_grants():
    ctx = stub_auth_context(
        org_role=OrganizationRole.OWNER,
        api_key_project_ids=frozenset({"a"}),
    )
    with (
        patch("sibyl.api.routes.search.verify_entity_project_access", AsyncMock()) as verify,
        patch(
            "sibyl.api.routes.search.list_accessible_project_graph_ids",
            AsyncMock(return_value={"a"}),
        ),
        patch("sibyl_core.tools.core.search", AsyncMock()) as core,
        pytest.raises(HTTPException) as denied,
    ):
        await search(
            SearchRequest(query="telescope", project="b"),
            org=SimpleNamespace(id=uuid4()),
            ctx=ctx,
        )
    assert denied.value.status_code == 403
    verify.assert_not_awaited()
    core.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_search_uses_owner_api_key_project_grants_for_explicit_widening():
    registry = ToolRegistry()
    register_retrieval_tools(registry)
    ctx = McpContext(
        org_id=str(uuid4()), user_id="owner", org_role="owner", api_key_project_ids=["a"]
    )
    with (
        patch("sibyl.mcp_tools.context.require_context", AsyncMock(return_value=ctx)),
        patch(
            "sibyl.mcp_tools.context.resolve_project_graph_grants",
            AsyncMock(return_value=(frozenset({"a"}), frozenset({"a"}))),
        ) as grants,
        patch(
            "sibyl_core.tools.core.search",
            AsyncMock(return_value=_SearchResult([], 0, "telescope")),
        ) as core,
    ):
        await registry.tools["search"]("telescope", all_projects=True)
    assert grants.await_args.kwargs["api_key_project_ids"] == ["a"]
    assert core.await_args.kwargs["accessible_projects"] == {"a"}
