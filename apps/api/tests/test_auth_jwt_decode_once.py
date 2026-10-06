"""A bearer is JWT-decoded at most once per request, and never when it is a key.

API keys used to pay two failing PyJWT decodes (middleware, then
resolve_claims again because the middleware produced None) plus a debug log
line before the sk_ branch ran, and a rejected session token was verified
twice for the same reason.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sibyl.auth import dependencies, middleware
from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.jwt import JwtError

API_KEY = "sk_live_not-a-jwt"
SESSION_TOKEN = "eyJhbGciOiJIUzI1NiJ9.expired.signature"


def _bearer_request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "state": {},
        }
    )


def _api_key_auth() -> ApiKeyAuth:
    return ApiKeyAuth(
        api_key_id=uuid4(), user_id=uuid4(), organization_id=uuid4(), scopes=["api:read"]
    )


@pytest.fixture
def verify(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """One counting verifier behind both modules that import the name."""
    counting = Mock(side_effect=JwtError("Signature has expired"))
    monkeypatch.setattr(dependencies, "verify_access_token", counting)
    monkeypatch.setattr(middleware, "verify_access_token", counting)
    return counting


def _claims_app() -> Starlette:
    async def claims(request: Request) -> JSONResponse:
        resolved = await dependencies.resolve_claims(request)
        return JSONResponse({"typ": None if resolved is None else resolved.get("typ")})

    app = Starlette(routes=[Route("/api/claims", claims)])
    app.add_middleware(middleware.AuthMiddleware)
    return app


@pytest.mark.asyncio
async def test_resolve_claims_never_jwt_decodes_an_api_key(
    verify: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dependencies, "authenticate_api_key", AsyncMock(return_value=_api_key_auth())
    )

    claims = await dependencies.resolve_claims(_bearer_request(API_KEY))

    assert claims is not None
    assert claims["typ"] == "api_key"
    verify.assert_not_called()


@pytest.mark.asyncio
async def test_middleware_never_jwt_decodes_an_api_key(
    verify: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dependencies, "authenticate_api_key", AsyncMock(return_value=_api_key_auth())
    )

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=_claims_app()), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/claims", headers={"Authorization": f"Bearer {API_KEY}"})

    assert response.json() == {"typ": "api_key"}
    verify.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_session_token_is_verified_once_per_request(verify: Mock) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=_claims_app()), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/claims", headers={"Authorization": f"Bearer {SESSION_TOKEN}"}
        )

    assert response.json() == {"typ": None}
    assert verify.call_count == 1


@pytest.mark.asyncio
async def test_resolve_claims_still_verifies_when_no_middleware_ran(
    verify: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct callers without the middleware keep the verification step."""
    verify.side_effect = None
    verify.return_value = {"sub": str(uuid4()), "typ": "access"}
    monkeypatch.setattr(dependencies, "validate_access_session", AsyncMock(return_value=True))

    claims = await dependencies.resolve_claims(_bearer_request(SESSION_TOKEN))

    assert claims is not None
    assert claims["typ"] == "access"
    verify.assert_called_once_with(SESSION_TOKEN)
