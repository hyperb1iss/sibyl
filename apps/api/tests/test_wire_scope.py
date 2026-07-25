"""Scope enforcement exercised through the real HTTP request path.

Every other scope test in this suite calls route functions directly, which
skips routing, dependency resolution and response serialization — the layers
where a missing reader or an unstamped payload actually arrives. These drive a
TestClient so a request crosses all of them, substituting only the auth
dependencies and the graph client.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_org_role,
    get_current_organization,
)
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole

ORG_ID = UUID("00000000-0000-0000-0000-000000000111")
OWNER_ID = UUID("00000000-0000-0000-0000-0000000000aa")
OUTSIDER_ID = UUID("00000000-0000-0000-0000-0000000000bb")
SECRET_NAME = "Acquisition target is Initech"
SECRET_TEXT = "board voted 5-2, do not disclose"


def _org() -> AuthOrganization:
    return AuthOrganization(id=ORG_ID, name="Sibyl", slug="sibyl")


def _ctx_for(user_id: UUID) -> AuthContext:
    return AuthContext(
        user=AuthUser(id=user_id, email=f"{user_id}@example.test"),
        organization=_org(),
        org_role=OrganizationRole.MEMBER,
    )


def _wire(router: Any, user_id: UUID) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_organization] = _org
    app.dependency_overrides[get_auth_context] = lambda: _ctx_for(user_id)
    app.dependency_overrides[get_current_org_role] = lambda: OrganizationRole.MEMBER
    return TestClient(app, raise_server_exceptions=False)


class TestTemporalWire:
    """POST /search/temporal returns endpoint names and the fact text.

    Unfiltered it was a whole-organization content dump: the widest read
    surface found in the sweep.
    """

    @staticmethod
    def _rows() -> list[dict[str, Any]]:
        return [
            {
                "edge_id": "edge-private",
                "name": "RELATED_TO",
                "fact": SECRET_TEXT,
                "source_id": "decision_private",
                "source_name": SECRET_NAME,
                "target_id": "episode_shared",
                "target_name": "Shared episode",
                "source_attributes": {
                    "memory_scope": "private",
                    "principal_id": str(OWNER_ID),
                },
                "target_attributes": {},
                "created_at": None,
                "expired_at": None,
                "valid_at": None,
                "invalid_at": None,
            },
            {
                "edge_id": "edge-shared",
                "name": "RELATED_TO",
                "fact": "public linkage",
                "source_id": "episode_shared",
                "source_name": "Shared episode",
                "target_id": "episode_other",
                "target_name": "Other shared",
                "source_attributes": {},
                "target_attributes": {},
                "created_at": None,
                "expired_at": None,
                "valid_at": None,
                "invalid_at": None,
            },
        ]

    def _request(self, user_id: UUID):
        from sibyl.api.routes.search import router

        rows = self._rows()

        class _Client:
            async def execute_query(self, _query: str, **_params: object):
                return rows

        runtime = SimpleNamespace(client=_Client())
        with (
            patch(
                "sibyl_core.tools.temporal.get_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value=set()),
            ),
            _wire(router, user_id) as client,
        ):
            return client.post(
                "/search/temporal",
                json={"mode": "timeline", "entity_id": "episode_shared", "limit": 50},
            )

    def test_non_owner_receives_no_private_edge(self) -> None:
        response = self._request(OUTSIDER_ID)

        assert response.status_code == 200
        assert [edge["id"] for edge in response.json()["edges"]] == ["edge-shared"]
        assert SECRET_NAME not in response.text
        assert SECRET_TEXT not in response.text

    def test_owner_still_receives_their_own_edge(self) -> None:
        response = self._request(OWNER_ID)

        assert response.status_code == 200
        assert [edge["id"] for edge in response.json()["edges"]] == [
            "edge-private",
            "edge-shared",
        ]

    def test_endpoint_metadata_never_reaches_the_response(self) -> None:
        """The bags are projected only to answer the scope question."""
        response = self._request(OWNER_ID)

        assert "source_attributes" not in response.text
        assert "target_attributes" not in response.text
        assert "memory_scope" not in response.text


@pytest.mark.parametrize("user_id", [OUTSIDER_ID, OWNER_ID])
def test_temporal_wire_rejects_nothing_by_accident(user_id: UUID) -> None:
    """A scope filter that 500s or 403s would look like a fix but break the route."""
    response = TestTemporalWire()._request(user_id)

    assert response.status_code == 200


class TestRagRelatedEntitiesWire:
    """GET /rag/pages/{id}/entities returns entity names and descriptions."""

    def _request(self, user_id: UUID):
        from sibyl.api.routes.rag import router

        private = SimpleNamespace(
            id="decision_private",
            name=SECRET_NAME,
            description=SECRET_TEXT,
            content=SECRET_TEXT,
            entity_type=SimpleNamespace(value="episode"),
            metadata={"memory_scope": "private", "principal_id": str(OWNER_ID)},
        )
        shared = SimpleNamespace(
            id="episode_shared",
            name="Shared episode",
            description="org visible",
            content="org visible",
            entity_type=SimpleNamespace(value="episode"),
            metadata={},
        )
        entity_manager = SimpleNamespace(
            search=AsyncMock(return_value=[(private, 0.9), (shared, 0.8)])
        )

        with (
            patch(
                "sibyl.api.routes.rag.get_entity_graph_runtime",
                AsyncMock(return_value=SimpleNamespace(entity_manager=entity_manager)),
            ),
            patch(
                "sibyl.api.routes.rag.list_accessible_project_graph_ids",
                AsyncMock(return_value=set()),
            ),
            patch("sibyl.api.routes.rag.get_content_read_session") as session,
            patch(
                "sibyl.api.routes.rag.get_crawled_document_for_org",
                AsyncMock(return_value=SimpleNamespace(title="Doc title")),
            ),
            _wire(router, user_id) as client,
        ):
            session.return_value.__aenter__ = AsyncMock(return_value=object())
            session.return_value.__aexit__ = AsyncMock(return_value=False)
            return client.get("/rag/pages/00000000-0000-0000-0000-0000000000cc/entities")

    def test_non_owner_sees_no_private_entity(self) -> None:
        response = self._request(OUTSIDER_ID)

        assert response.status_code == 200
        assert SECRET_NAME not in response.text
        assert SECRET_TEXT not in response.text

    def test_owner_still_sees_their_own_entity(self) -> None:
        response = self._request(OWNER_ID)

        assert response.status_code == 200
        assert SECRET_NAME in response.text


class TestProjectMetricsWire:
    """GET /metrics/projects/{id} summarized any project for any org member."""

    def _request(self, *, member: bool):
        from sibyl.api.routes.metrics import router
        from sibyl.auth.errors import ProjectAccessDeniedError

        acl = (
            AsyncMock(return_value=None)
            if member
            else AsyncMock(side_effect=ProjectAccessDeniedError("proj-x", "viewer", None))
        )
        service = AsyncMock()
        service.get_entity.return_value = SimpleNamespace(id="proj-x", name="P", metadata={})
        runtime = SimpleNamespace(
            entity_manager=SimpleNamespace(list_by_type=AsyncMock(return_value=[]))
        )

        with (
            patch("sibyl.api.routes.metrics.verify_entity_project_access", acl),
            patch(
                "sibyl.api.routes.metrics.get_knowledge_read_adapter",
                AsyncMock(return_value=service),
            ),
            patch(
                "sibyl.api.routes.metrics.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            _wire(router, OUTSIDER_ID) as client,
        ):
            return client.get("/metrics/projects/proj-x")

    def test_non_member_is_refused(self) -> None:
        assert self._request(member=False).status_code == 403

    def test_member_still_gets_metrics(self) -> None:
        assert self._request(member=True).status_code == 200


class TestRestrictedApiKeyWire:
    """A narrowed API key must not regain a memory space by another route.

    `private_scope_granted` used to default True, so any call site that did not
    pass it silently granted private access. Only native retrieval passed it.
    """

    @staticmethod
    def _rows() -> list[dict[str, Any]]:
        return [
            {
                "edge_id": "edge-private",
                "name": "RELATED_TO",
                "fact": SECRET_TEXT,
                "source_id": "decision_private",
                "source_name": SECRET_NAME,
                "target_id": "episode_shared",
                "target_name": "Shared episode",
                "source_attributes": {
                    "memory_scope": "private",
                    "principal_id": str(OWNER_ID),
                },
                "target_attributes": {},
                "created_at": None,
                "expired_at": None,
                "valid_at": None,
                "invalid_at": None,
            }
        ]

    def _request(self, grants: set[str] | None):
        from sibyl.api.routes.search import router

        rows = self._rows()

        class _Client:
            async def execute_query(self, _query: str, **_params: object):
                return rows

        ctx = AuthContext(
            user=AuthUser(id=OWNER_ID, email="owner@example.test"),
            organization=_org(),
            org_role=OrganizationRole.MEMBER,
            api_key_memory_scope_keys=grants,
        )
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_organization] = _org
        app.dependency_overrides[get_auth_context] = lambda: ctx
        app.dependency_overrides[get_current_org_role] = lambda: OrganizationRole.MEMBER

        with (
            patch(
                "sibyl_core.tools.temporal.get_graph_runtime",
                AsyncMock(return_value=SimpleNamespace(client=_Client())),
            ),
            patch(
                "sibyl.api.routes.search.list_accessible_project_graph_ids",
                AsyncMock(return_value=set()),
            ),
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            return client.post(
                "/search/temporal",
                json={"mode": "timeline", "entity_id": "episode_shared"},
            )

    def test_project_only_key_cannot_read_its_owners_private_rows(self) -> None:
        from sibyl_core.auth.memory_policy import memory_scope_policy_key
        from sibyl_core.models.memory_scope import MemoryScope

        response = self._request({memory_scope_policy_key(MemoryScope.PROJECT, "proj-1")})

        assert response.status_code == 200
        assert SECRET_NAME not in response.text

    def test_key_granting_private_still_reads_them(self) -> None:
        from sibyl_core.auth.memory_policy import memory_scope_policy_key
        from sibyl_core.models.memory_scope import MemoryScope

        response = self._request({memory_scope_policy_key(MemoryScope.PRIVATE, str(OWNER_ID))})

        assert response.status_code == 200
        assert SECRET_NAME in response.text

    def test_full_session_is_unaffected(self) -> None:
        response = self._request(None)

        assert response.status_code == 200
        assert SECRET_NAME in response.text


class TestInaccessibleProjectWire:
    """Work items are deliberately unstamped, so their project is the gate."""

    def _nodes(self, accessible: set[str]):
        from sibyl.api.routes import graph as graph_routes
        from sibyl_core.models.entities import EntityType

        victim = SimpleNamespace(
            id="task_victim",
            entity_type=EntityType.TASK,
            name=SECRET_NAME,
            description=SECRET_TEXT,
            metadata={"project_id": "proj-victim"},
        )
        mine = SimpleNamespace(
            id="task_mine",
            entity_type=EntityType.TASK,
            name="My task",
            description="ordinary",
            metadata={"project_id": "proj-mine"},
        )
        runtime = SimpleNamespace(
            entity_manager=SimpleNamespace(list_all=AsyncMock(return_value=[victim, mine]))
        )
        adapter = SimpleNamespace(get_connection_counts=AsyncMock(return_value={}))

        with (
            patch(
                "sibyl.api.routes.graph.get_entity_graph_runtime",
                AsyncMock(return_value=runtime),
            ),
            patch(
                "sibyl.api.routes.graph.get_graph_query_adapter",
                AsyncMock(return_value=adapter),
            ),
            patch(
                "sibyl.api.routes.graph.list_accessible_project_graph_ids",
                AsyncMock(return_value=accessible),
            ),
            _wire(graph_routes.router, OUTSIDER_ID) as client,
        ):
            return client.get("/graph/nodes?limit=50")

    def test_nodes_exclude_a_project_the_reader_is_not_in(self) -> None:
        response = self._nodes({"proj-mine"})

        assert response.status_code == 200
        assert "task_victim" not in response.text
        assert SECRET_NAME not in response.text
        assert "task_mine" in response.text

    def test_hierarchical_refuses_a_project_the_caller_named(self) -> None:
        from sibyl.api.routes import graph as graph_routes

        captured: dict[str, Any] = {}

        async def _hierarchical(_client, _group_id, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                nodes=[],
                edges=[],
                clusters=[],
                cluster_edges=[],
                total_nodes=0,
                total_edges=0,
                displayed_nodes=0,
                displayed_edges=0,
                resolution="detail",
                recommended_resolution="detail",
            )

        with (
            patch(
                "sibyl.api.routes.graph.get_entity_graph_runtime",
                AsyncMock(return_value=SimpleNamespace(client=object())),
            ),
            patch("sibyl.api.routes.graph.get_hierarchical_graph", _hierarchical),
            patch(
                "sibyl.api.routes.graph.list_accessible_project_graph_ids",
                AsyncMock(return_value={"proj-mine"}),
            ),
            _wire(graph_routes.router, OUTSIDER_ID) as client,
        ):
            response = client.get("/graph/hierarchical?projects=proj-victim")

        assert response.status_code == 200
        assert "proj-victim" not in (captured.get("project_ids") or [])
