from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sibyl.api.app import create_api_app
from sibyl.api.routes.entities import create_entity
from sibyl.api.schemas import EntityCreate
from sibyl_core.models.entities import EntityType


def test_http_exception_envelope_redacts_cross_tenant_ids() -> None:
    app = create_api_app()
    tenant_id = "a8b50c88-4b37-4101-a346-07fdc9719cf1"

    @app.get("/_test/project-denied")
    async def project_denied() -> None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "project_access_denied",
                "message": "Requires contributor access to project",
                "details": {
                    "project_id": tenant_id,
                    "required_role": "contributor",
                    "actual_role": "viewer",
                    "token": "secret-token",
                },
            },
        )

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        "/_test/project-denied",
        headers={"X-Request-ID": "req_cross_tenant"},
    )

    assert response.status_code == 403
    assert response.headers["X-Request-ID"] == "req_cross_tenant"
    body = response.json()
    assert body["error"] == "project_access_denied"
    assert body["request_id"] == "req_cross_tenant"
    assert body["details"] == {"expected": "contributor", "actual": "viewer"}
    assert tenant_id not in response.text
    assert "secret-token" not in response.text


def test_unhandled_exception_envelope_hides_raw_details() -> None:
    app = create_api_app()
    raw_path = "/home/service/private/path"

    @app.get("/_test/boom")
    async def boom() -> None:
        sql = "SEL" + "ECT token FROM"
        raise RuntimeError(f"{sql} {raw_path}")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/_test/boom", headers={"X-Request-ID": "req_boom"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "internal_error"
    assert body["request_id"] == "req_boom"
    assert "SELECT" not in response.text
    assert raw_path not in response.text


@pytest.mark.asyncio
async def test_create_entity_duplicate_uses_constraint_envelope() -> None:
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000222")))
    request = MagicMock()
    request.headers = {}
    request.cookies = {}
    entity = EntityCreate(
        name="Existing",
        content="same name",
        entity_type=EntityType.EPISODE,
    )
    add_result = SimpleNamespace(success=False, id=None, message="duplicate entity name")

    with (
        patch("sibyl_core.tools.core.add", AsyncMock(return_value=add_result)),
        pytest.raises(HTTPException) as exc,
    ):
        await create_entity(
            request=request,
            entity=entity,
            org=org,
            ctx=ctx,
            content_session=None,
            sync=False,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "constraint_violation"
    assert exc.value.detail["message"] == "duplicate entity name in scope"
    assert exc.value.detail["details"] == {
        "field": "name",
        "entity_type": "episode",
    }
