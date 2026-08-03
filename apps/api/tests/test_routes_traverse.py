"""Route contract for the bounded traversal verbs.

The routes own one job the core cannot do for them: resolving which projects the
caller may read and handing that set, the principal, and any API-key narrowing
to the walk. A route that resolves the wrong set produces a correctly authorized
walk over the wrong audience, so these assert the arguments that cross the seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.search import expand_neighbors, fetch_slice
from sibyl.api.schemas import ExpandNeighborsRequest, FetchSliceRequest
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl_core.auth import OrganizationRole, ProjectRole
from tests.harness.auth import stub_auth_context

ORG = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))


@dataclass
class _Neighbor:
    id: str
    type: str = "decision"
    name: str = "Neighbor"
    relationship: str = "RELATED_TO"
    direction: str = "outgoing"
    distance: int = 1
    score: float = 0.7
    content: str = "preview"
    project_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class _ExpandResult:
    origins: list[str]
    neighbors: list[_Neighbor]
    total: int
    depth: int
    limit: int
    unresolved: list[str] = field(default_factory=list)
    truncated: bool = False
    filters: dict[str, object] = field(default_factory=dict)
    usage_hint: str = "bounded"


@dataclass
class _Passage:
    id: str
    name: str = "span"
    content: str = "span body"
    passage_index: int | None = 0
    passage_total: int | None = 3
    breadcrumb: str | None = None
    truncated: bool = False


@dataclass
class _SliceResult:
    entity_id: str
    parent_id: str
    parent_name: str
    parent_type: str
    passages: list[_Passage]
    window: int
    sliced: bool
    total: int = 0
    window_start: int | None = None
    passage_total: int | None = None
    covers_parent: bool = False
    project_id: str | None = None
    content_chars: int = 0
    filters: dict[str, object] = field(default_factory=dict)
    usage_hint: str = "bounded"


def _expand_result() -> _ExpandResult:
    return _ExpandResult(
        origins=["decision_seed"],
        neighbors=[_Neighbor(id="decision_neighbor")],
        total=1,
        depth=1,
        limit=8,
    )


def _slice_result() -> _SliceResult:
    return _SliceResult(
        entity_id="decision_parent",
        parent_id="decision_parent",
        parent_name="Parent",
        parent_type="decision",
        passages=[_Passage(id="decision_parent_passage_0")],
        window=3,
        sliced=True,
        total=1,
    )


class TestExpandNeighborsRoute:
    @pytest.mark.asyncio
    async def test_passes_the_readers_full_project_set_when_none_named(self) -> None:
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_a", "proj_b"}),
            ) as list_projects,
            patch(
                "sibyl_core.tools.core.expand_neighbors",
                AsyncMock(return_value=_expand_result()),
            ) as core_expand,
        ):
            response = await expand_neighbors(
                request=ExpandNeighborsRequest(entity_ids=["decision_seed"]),
                org=ORG,
                ctx=ctx,
            )

        list_projects.assert_awaited_once_with(ctx)
        assert response.total == 1
        assert response.neighbors[0].id == "decision_neighbor"
        kwargs = core_expand.await_args.kwargs
        assert kwargs["accessible_projects"] == {"proj_a", "proj_b"}
        assert kwargs["principal_id"] == str(ctx.user_id)
        assert kwargs["organization_id"] == str(ORG.id)

    @pytest.mark.asyncio
    async def test_named_project_is_verified_and_becomes_the_scope(self) -> None:
        """None would empty the walk, so the verified project has to reach the core."""
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(return_value=None),
            ) as verify,
            patch(
                "sibyl_core.tools.core.expand_neighbors",
                AsyncMock(return_value=_expand_result()),
            ) as core_expand,
        ):
            await expand_neighbors(
                request=ExpandNeighborsRequest(entity_ids=["decision_seed"], project="proj_a"),
                org=ORG,
                ctx=ctx,
            )

        assert verify.await_args.args[2] == "proj_a"
        assert verify.await_args.kwargs["required_role"] == ProjectRole.VIEWER
        assert core_expand.await_args.kwargs["accessible_projects"] == {"proj_a"}

    @pytest.mark.asyncio
    async def test_project_access_denial_propagates(self) -> None:
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(
                    side_effect=ProjectAccessDeniedError(
                        project_id="proj_forbidden",
                        required_role="viewer",
                    )
                ),
            ),
            patch("sibyl_core.tools.core.expand_neighbors", AsyncMock()) as core_expand,
            pytest.raises(ProjectAccessDeniedError),
        ):
            await expand_neighbors(
                request=ExpandNeighborsRequest(
                    entity_ids=["decision_seed"], project="proj_forbidden"
                ),
                org=ORG,
                ctx=ctx,
            )

        core_expand.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_api_key_memory_grants_reach_the_walk(self) -> None:
        ctx = stub_auth_context(
            org_role=OrganizationRole.MEMBER,
            api_key_memory_scope_keys={"project:proj_a"},
        )

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_a"}),
            ),
            patch(
                "sibyl_core.tools.core.expand_neighbors",
                AsyncMock(return_value=_expand_result()),
            ) as core_expand,
        ):
            await expand_neighbors(
                request=ExpandNeighborsRequest(entity_ids=["decision_seed"]),
                org=ORG,
                ctx=ctx,
            )

        assert core_expand.await_args.kwargs["allowed_memory_scope_keys"] == {"project:proj_a"}

    @pytest.mark.asyncio
    async def test_unnarrowed_session_sends_no_grant_restriction(self) -> None:
        """None means unrestricted downstream, so it must not be a set of nothing."""
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_a"}),
            ),
            patch(
                "sibyl_core.tools.core.expand_neighbors",
                AsyncMock(return_value=_expand_result()),
            ) as core_expand,
        ):
            await expand_neighbors(
                request=ExpandNeighborsRequest(entity_ids=["decision_seed"]),
                org=ORG,
                ctx=ctx,
            )

        assert core_expand.await_args.kwargs["allowed_memory_scope_keys"] is None

    @pytest.mark.asyncio
    async def test_walk_failure_is_a_500_not_a_leak(self) -> None:
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_a"}),
            ),
            patch(
                "sibyl_core.tools.core.expand_neighbors",
                AsyncMock(side_effect=RuntimeError("surreal exploded")),
            ),
            pytest.raises(HTTPException) as raised,
        ):
            await expand_neighbors(
                request=ExpandNeighborsRequest(entity_ids=["decision_seed"]),
                org=ORG,
                ctx=ctx,
            )

        assert raised.value.status_code == 500
        assert "surreal exploded" not in str(raised.value.detail)


class TestExpandNeighborsRequestBounds:
    def test_seed_count_is_capped_at_the_core_budget(self) -> None:
        from sibyl_core.tools.traverse import MAX_EXPAND_ORIGINS

        with pytest.raises(ValueError):
            ExpandNeighborsRequest(
                entity_ids=[f"seed_{index}" for index in range(MAX_EXPAND_ORIGINS + 1)]
            )

    def test_depth_and_limit_reject_values_past_the_ceiling(self) -> None:
        from sibyl_core.tools.traverse import MAX_EXPAND_LIMIT, MAX_TRAVERSAL_DEPTH

        with pytest.raises(ValueError):
            ExpandNeighborsRequest(entity_ids=["seed"], depth=MAX_TRAVERSAL_DEPTH + 1)
        with pytest.raises(ValueError):
            ExpandNeighborsRequest(entity_ids=["seed"], limit=MAX_EXPAND_LIMIT + 1)

    def test_inbound_edges_are_followed_by_default(self) -> None:
        assert ExpandNeighborsRequest(entity_ids=["seed"]).include_incoming is True


class TestFetchSliceRoute:
    @pytest.mark.asyncio
    async def test_returns_the_window_and_the_parent_it_cites(self) -> None:
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_a"}),
            ),
            patch(
                "sibyl_core.tools.core.fetch_slice",
                AsyncMock(return_value=_slice_result()),
            ) as core_slice,
        ):
            response = await fetch_slice(
                request=FetchSliceRequest(entity_id="decision_parent"),
                org=ORG,
                ctx=ctx,
            )

        assert response.parent_id == "decision_parent"
        assert response.sliced is True
        assert len(response.passages) == 1
        kwargs = core_slice.await_args.kwargs
        assert kwargs["accessible_projects"] == {"proj_a"}
        assert kwargs["principal_id"] == str(ctx.user_id)

    @pytest.mark.asyncio
    async def test_unreadable_or_absent_id_is_a_404(self) -> None:
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_a"}),
            ),
            patch(
                "sibyl_core.tools.core.fetch_slice",
                AsyncMock(side_effect=KeyError("decision_secret")),
            ),
            pytest.raises(HTTPException) as raised,
        ):
            await fetch_slice(
                request=FetchSliceRequest(entity_id="decision_secret"),
                org=ORG,
                ctx=ctx,
            )

        assert raised.value.status_code == 404
        # The id must not appear: a distinct message for a denied row confirms it.
        assert "decision_secret" not in str(raised.value.detail)

    @pytest.mark.asyncio
    async def test_named_project_is_verified_before_the_read(self) -> None:
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.verify_entity_project_access",
                AsyncMock(return_value=None),
            ) as verify,
            patch(
                "sibyl_core.tools.core.fetch_slice",
                AsyncMock(return_value=_slice_result()),
            ) as core_slice,
        ):
            await fetch_slice(
                request=FetchSliceRequest(entity_id="decision_parent", project="proj_a"),
                org=ORG,
                ctx=ctx,
            )

        assert verify.await_args.args[2] == "proj_a"
        assert core_slice.await_args.kwargs["accessible_projects"] == {"proj_a"}

    @pytest.mark.asyncio
    async def test_read_failure_is_a_500(self) -> None:
        ctx = stub_auth_context()

        with (
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj_a"}),
            ),
            patch(
                "sibyl_core.tools.core.fetch_slice",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            pytest.raises(HTTPException) as raised,
        ):
            await fetch_slice(
                request=FetchSliceRequest(entity_id="decision_parent"),
                org=ORG,
                ctx=ctx,
            )

        assert raised.value.status_code == 500


class TestFetchSliceRequestBounds:
    def test_window_defaults_to_the_measured_adjacency(self) -> None:
        from sibyl_core.retrieval.operational_sources import PASSAGE_WINDOW_UNITS

        assert FetchSliceRequest(entity_id="x").window == PASSAGE_WINDOW_UNITS

    def test_window_rejects_values_past_the_span_cap(self) -> None:
        from sibyl_core.tools.traverse import MAX_SLICE_WINDOW

        with pytest.raises(ValueError):
            FetchSliceRequest(entity_id="x", window=MAX_SLICE_WINDOW + 1)

    def test_entity_id_cannot_be_empty(self) -> None:
        with pytest.raises(ValueError):
            FetchSliceRequest(entity_id="")


class TestTraversalToolsAreRegistered:
    @pytest.mark.asyncio
    async def test_mcp_exposes_both_verbs(self) -> None:
        from sibyl.server import create_mcp_server

        mcp = create_mcp_server()
        tools = {tool.name for tool in await mcp.list_tools()}
        assert {"expand_neighbors", "fetch_slice"} <= tools

    @pytest.mark.asyncio
    async def test_docstrings_state_the_round_budget(self) -> None:
        """The docstring is the only place the bound is visible to an agent."""
        from sibyl.server import create_mcp_server

        mcp = create_mcp_server()
        by_name = {tool.name: tool for tool in await mcp.list_tools()}
        for name in ("expand_neighbors", "fetch_slice"):
            description = by_name[name].description or ""
            assert "THREE ROUNDS" in description
            assert "context" in description
