"""Tests for the auth-flow acceptance harness."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from sibyl.cli.auth_flow import (
    AuthFlowError,
    AuthFlowResult,
    AuthFlowTokenClaims,
    compare_auth_flow_results,
    replay_auth_flow,
)


def _json_request(request: httpx.Request) -> dict[str, object]:
    if not request.content:
        return {}
    if "application/json" not in request.headers.get("content-type", ""):
        return {}
    payload: object = json.loads(request.content.decode("utf-8"))
    assert isinstance(payload, dict)
    return {str(key): value for key, value in payload.items()}


def _json_response(status_code: int, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _jwt_segment(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _jwt_token(
    *,
    label: str,
    typ: str,
    org: str = "org-primary",
    sid: bool = True,
) -> str:
    payload: dict[str, object] = {"sub": f"user-{label}", "typ": typ, "org": org}
    if sid:
        payload["sid"] = f"session-{label}"
    if typ == "refresh":
        payload["jti"] = f"jti-{label}"
    return f"{_jwt_segment({'alg': 'none', 'typ': 'JWT'})}.{_jwt_segment(payload)}.sig"


def _access(label: str, *, org: str = "org-primary", sid: bool = True) -> str:
    return _jwt_token(label=label, typ="access", org=org, sid=sid)


def _refresh(label: str, *, org: str = "org-primary", sid: bool = True) -> str:
    return _jwt_token(label=label, typ="refresh", org=org, sid=sid)


@pytest.mark.asyncio
async def test_replay_auth_flow_exercises_cutover_auth_surface(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []
    api_key_revoked = False
    device_approved = False
    logged_out_authorization = ""
    outbox_path = tmp_path / "email-outbox.jsonl"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal api_key_revoked, device_approved, logged_out_authorization

        seen.append((request.method, request.url.path))
        path = request.url.path
        body = _json_request(request)
        authorization = request.headers.get("authorization", "")

        if path == "/api/auth/local/signup":
            email = str(body["email"])
            if "+member@" in email:
                return _json_response(
                    201,
                    {
                        "user": {"id": "user-secondary", "email": email},
                        "organization": {
                            "id": "org-secondary",
                            "slug": "secondary-org",
                            "name": "Secondary",
                        },
                        "access_token": _access("secondary", org="org-secondary"),
                        "refresh_token": _refresh("secondary", org="org-secondary"),
                    },
                )
            return _json_response(
                201,
                {
                    "user": {"id": "user-primary", "email": email},
                    "organization": {
                        "id": "org-primary",
                        "slug": "primary-org",
                        "name": "Primary",
                    },
                    "access_token": _access("primary-signup"),
                    "refresh_token": _refresh("primary-signup"),
                },
            )

        if path == "/api/auth/local/login":
            password = str(body["password"])
            if password.endswith("-rotated"):
                token = "primary-rotated"
            elif password.endswith("-reset"):
                token = "primary-reset"
            else:
                token = "primary-login"
            return _json_response(
                200,
                {
                    "user": {"id": "user-primary", "email": body["email"]},
                    "organization": {
                        "id": "org-primary",
                        "slug": "primary-org",
                        "name": "Primary",
                    },
                    "access_token": _access(token),
                    "refresh_token": _refresh(token),
                },
            )

        if path == "/api/auth/refresh":
            return _json_response(
                200,
                {
                    "access_token": _access("primary-refresh"),
                    "refresh_token": _refresh("primary-refresh"),
                },
            )

        if path == "/api/auth/api-keys":
            return _json_response(
                200,
                {"id": "00000000-0000-0000-0000-000000000111", "api_key": "sk_test_key"},
            )

        if path == "/api/auth/me":
            if authorization and authorization == logged_out_authorization:
                return _json_response(401, {"detail": "Session revoked"})
            if authorization == "Bearer sk_test_key" and api_key_revoked:
                return _json_response(401, {"detail": "Invalid API key"})
            return _json_response(
                200,
                {
                    "user": {"id": "user-primary"},
                    "organization": {"id": "org-primary"},
                    "org_role": "owner",
                },
            )

        if path == "/api/auth/api-keys/00000000-0000-0000-0000-000000000111/revoke":
            api_key_revoked = True
            return _json_response(200, {"success": True})

        if path == "/api/orgs/primary-org/invitations":
            return _json_response(
                200,
                {
                    "invitation": {
                        "id": "invite-id",
                        "email": body["email"],
                        "role": "member",
                        "accept_url": "http://localhost/api/invitations/invite-token/accept",
                    }
                },
            )

        if path == "/api/invitations/invite-token/accept":
            return _json_response(
                200,
                {
                    "access_token": _access("secondary-primary"),
                    "refresh_token": _refresh("secondary-primary"),
                    "organization_id": "org-primary",
                },
            )

        if path == "/api/orgs/secondary-org/switch":
            return _json_response(
                200,
                {
                    "organization": {"id": "org-secondary", "slug": "secondary-org"},
                    "access_token": _access("secondary-personal", org="org-secondary"),
                    "refresh_token": _refresh("secondary-personal", org="org-secondary"),
                },
            )

        if path == "/api/orgs/primary-org/switch":
            return _json_response(
                200,
                {
                    "organization": {"id": "org-primary", "slug": "primary-org"},
                    "access_token": _access("secondary-primary-return"),
                    "refresh_token": _refresh("secondary-primary-return"),
                },
            )

        if path == "/api/auth/device":
            return _json_response(
                200,
                {"device_code": "device-code-123", "user_code": "ABCD-EFGH"},
            )

        if path == "/api/auth/device/token":
            if device_approved:
                return _json_response(
                    200,
                    {"access_token": _access("device"), "refresh_token": _refresh("device")},
                )
            return _json_response(400, {"error": "authorization_pending"})

        if path == "/api/auth/device/verify":
            device_approved = True
            return httpx.Response(200, text="approved")

        if path == "/api/users/me/password":
            return httpx.Response(204)

        if path == "/api/users/password/reset":
            outbox_path.write_text(
                json.dumps(
                    {
                        "to": [body["email"]],
                        "subject": "Reset your Sibyl password",
                        "html": "/reset-password?token=reset-token",
                        "text": "http://localhost/reset-password?token=reset-token",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return _json_response(
                202, {"message": "If an account exists, a reset email has been sent."}
            )

        if path == "/api/users/password/reset/confirm":
            assert body["token"] == "reset-token"
            return httpx.Response(204)

        if path == "/api/users/me/sessions":
            return _json_response(
                200,
                {
                    "sessions": [
                        {
                            "id": "session-id",
                            "created_at": "2026-05-04T00:00:00Z",
                            "expires_at": "2026-05-05T00:00:00Z",
                            "is_current": True,
                        }
                    ]
                },
            )

        if path == "/api/auth/logout":
            logged_out_authorization = authorization
            return httpx.Response(204)

        return _json_response(404, {"detail": f"Unhandled {path}"})

    result = await replay_auth_flow(
        base_url="http://sibyl.test",
        email="auth-flow@example.com",
        password="auth-flow-password-secure-123!",
        email_outbox_path=outbox_path,
        transport=httpx.MockTransport(handler),
    )

    assert result.primary_email == "auth-flow@example.com"
    assert result.secondary_email == "auth-flow+member@example.com"
    assert result.organization_slug == "primary-org"
    assert result.steps == (
        "signup_primary_user",
        "login_primary_user",
        "refresh_tokens",
        "create_api_key",
        "authenticate_api_key",
        "revoke_api_key",
        "signup_invited_user",
        "invite_and_accept_user",
        "switch_active_org",
        "device_auth_flow",
        "change_password",
        "password_reset_request_and_consume",
        "list_user_sessions",
        "logout_rejects_access_token",
    )
    assert ("POST", "/api/auth/local/signup") in seen
    assert ("POST", "/api/users/password/reset") in seen
    assert api_key_revoked is True
    assert device_approved is True
    assert len(result.token_claims) == 20
    assert {claims.typ for claims in result.token_claims} == {"access", "refresh"}
    assert all(claims.has_sub for claims in result.token_claims)
    assert all(claims.has_org for claims in result.token_claims)
    assert all(claims.has_sid for claims in result.token_claims)


@pytest.mark.asyncio
async def test_replay_auth_flow_reports_missing_token() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(201, {"organization": {"slug": "primary-org"}})

    with pytest.raises(AuthFlowError, match="access_token"):
        await replay_auth_flow(
            base_url="http://sibyl.test",
            email="auth-flow@example.com",
            password="auth-flow-password-secure-123!",
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.asyncio
async def test_replay_auth_flow_requires_sid_claim() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            201,
            {
                "organization": {"slug": "primary-org"},
                "access_token": _access("primary-signup", sid=False),
                "refresh_token": _refresh("primary-signup"),
            },
        )

    with pytest.raises(AuthFlowError, match="missing required JWT claims: sid"):
        await replay_auth_flow(
            base_url="http://sibyl.test",
            email="auth-flow@example.com",
            password="auth-flow-password-secure-123!",
            transport=httpx.MockTransport(handler),
        )


def test_compare_auth_flow_results_detects_jwt_shape_mismatch() -> None:
    left = AuthFlowResult(
        primary_email="left@example.com",
        secondary_email="left+member@example.com",
        organization_slug="left",
        steps=("login_primary_user",),
        token_claims=(
            AuthFlowTokenClaims(
                step="login primary user",
                field_name="access_token",
                typ="access",
                has_sub=True,
                has_org=True,
                has_sid=True,
                has_jti=False,
            ),
        ),
    )
    right = AuthFlowResult(
        primary_email="right@example.com",
        secondary_email="right+member@example.com",
        organization_slug="right",
        steps=("login_primary_user",),
        token_claims=(
            AuthFlowTokenClaims(
                step="login primary user",
                field_name="access_token",
                typ="access",
                has_sub=True,
                has_org=True,
                has_sid=False,
                has_jti=False,
            ),
        ),
    )

    with pytest.raises(AuthFlowError, match="JWT claim shape differed"):
        compare_auth_flow_results(
            left_label="postgres",
            left=left,
            right_label="surreal",
            right=right,
        )
