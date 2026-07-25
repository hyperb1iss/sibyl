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
