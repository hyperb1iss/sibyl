"""Every mounted route must bind a caller identity of its own.

Scope tests elsewhere in this suite override the auth dependencies, so a route
that never declared them looks exactly like a route that did. This test reads
the mounted application instead of calling it, which is the only way a missing
dependency becomes visible.

The rule is deliberately stricter than "the route is authenticated". A
router-level gate such as require_org_admin() proves the caller administers
some organization, and every user owns a personal organization with the OWNER
role, so inheriting that gate proves nothing about whose data the handler is
about to serve. Only a dependency the route declares for itself tells the
handler which caller, and which organization, it is answering for.
"""

from __future__ import annotations

from typing import Any

from fastapi import params
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute, APIWebSocketRoute

from sibyl.api.app import create_api_app
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_org_role,
    get_current_organization,
    get_current_user,
)
from sibyl.persistence.operations_runtime import (
    require_global_admin,
    require_setup_mode_or_admin,
    require_setup_mode_or_auth,
)

# Dependencies that resolve who is calling. Role gates such as require_org_admin
# and require_org_role are deliberately absent: they authorize a caller against
# an organization someone else already resolved.
CALLER_IDENTITY_DEPENDENCIES = frozenset(
    {
        get_auth_context,
        get_current_org_role,
        get_current_organization,
        get_current_user,
        require_global_admin,
        require_setup_mode_or_admin,
        require_setup_mode_or_auth,
    }
)

# Routes that answer without knowing the caller, keyed by "<methods> <path>".
# Adding an entry here is a decision to serve a route to anyone who reaches it,
# so each one records why that is safe.
ROUTES_WITHOUT_CALLER_IDENTITY: dict[str, str] = {
    # Service surface, no stored data.
    "GET /": "version banner",
    "GET /health": "liveness probe",
    "GET /health/ready": "readiness probe",
    "GET,HEAD /openapi.json": "generated API schema",
    "GET,HEAD /docs": "Swagger UI shell",
    "GET,HEAD /docs/oauth2-redirect": "Swagger UI OAuth redirect shim",
    "GET,HEAD /redoc": "ReDoc UI shell",
    # Establishing a session: the caller has no identity yet.
    "GET /auth/providers": "lists configured login providers",
    "GET /auth/github": "starts the GitHub OAuth redirect",
    "GET /auth/github/callback": "GitHub OAuth callback, state-verified",
    "GET /auth/oidc/{provider_name}/login": "starts the OIDC redirect",
    "GET /auth/oidc/{provider_name}/callback": "OIDC callback, state-verified",
    "GET /auth/oidc/{provider_name}/refresh": "OIDC silent refresh, cookie-verified",
    "POST /auth/local/signup": "creates the first account for an email",
    "POST /auth/local/login": "exchanges credentials for a session",
    "POST /auth/device": "starts the device authorization grant",
    "POST /auth/device/token": "polls the device grant for a token",
    "GET /auth/device/verify": "renders the device code confirmation page",
    "POST /auth/device/verify": "confirms a device code, session-verified",
    "POST /auth/refresh": "rotates a session from the refresh cookie",
    "POST /auth/logout": "clears session cookies",
    "POST /users/password/reset": "sends a reset mail for an email address",
    "POST /users/password/reset/confirm": "redeems a reset token",
    "GET /setup/status": "reports whether the instance is unconfigured",
    # Instance-wide settings, gated in the handler body by require_settings_owner
    # rather than by a dependency (see AUDIT_2026-08-13 debt-api finding 6).
    "GET /settings": "require_settings_owner called in the handler",
    "PATCH /settings": "require_settings_owner called in the handler",
    "DELETE /settings/{key}": "require_settings_owner called in the handler",
    "GET /settings/ai/llm": "require_settings_owner called in the handler",
    "PUT /settings/ai/llm/{surface}": "require_settings_owner called in the handler",
    "PUT /settings/ai/llm-budget": "require_settings_owner called in the handler",
    "POST /settings/ai/llm/{surface}/test": "require_settings_owner called in the handler",
    "POST /settings/ai/keys/{provider}/test": "require_settings_owner called in the handler",
    "POST /settings/ai/models/{model_alias}/test": "require_settings_owner called in the handler",
    "GET /settings/ai/registry": "require_settings_owner called in the handler",
    # WebSockets cannot use HTTP dependencies and authenticate in the handler.
    "WS /ws": "token checked in websocket_handler",
    "WS /logs/stream": "token checked in the stream handler",
    # Org-independent answers: no row, document or job of any org is reachable.
    "GET /jobs/health": "broker and queue health counters",
    "GET /sources/health": "crawler subsystem health",
    "GET /sources/preview": "fetches a caller-supplied URL, reads nothing stored",
    "GET /ingestion/import-adapters": "static list of compiled-in import adapters",
}

# A walk that silently returns nothing would pass every assertion below.
MINIMUM_MOUNTED_ROUTES = 150


def _resolved_calls(dependant: Any, found: set[Any]) -> set[Any]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            found.add(sub.call)
        _resolved_calls(sub, found)
    return found


def _calls_of(dependencies: list[params.Depends]) -> set[Any]:
    found: set[Any] = set()
    for dependency in dependencies:
        call = dependency.dependency
        if call is None:
            continue
        found.add(call)
        _resolved_calls(get_dependant(path="/", call=call), found)
    return found


def _route_key(route: Any, path: str) -> str:
    methods = getattr(route, "methods", None)
    label = ",".join(sorted(methods)) if methods else "WS"
    return f"{label} {path}"


def _mounted_routes(
    routes: list[Any],
    prefix: str = "",
    inherited: frozenset[Any] = frozenset(),
) -> list[tuple[str, Any, frozenset[Any]]]:
    """Flatten the app into leaf routes, tracking what each router applies."""
    flattened: list[tuple[str, Any, frozenset[Any]]] = []
    for route in routes:
        # FastAPI mounts an included router as a single node rather than copying
        # its routes, so the router's own dependencies live on the node.
        include = getattr(route, "include_context", None)
        if include is not None:
            router = include.included_router
            below = (
                set(inherited) | _calls_of(include.dependencies) | _calls_of(router.dependencies)
            )
            flattened += _mounted_routes(router.routes, prefix + include.prefix, frozenset(below))
            continue

        path = prefix + getattr(route, "path", "")
        flattened.append((path, route, inherited))
    return flattened


def _self_declared_identity(route: Any, inherited: frozenset[Any]) -> set[Any]:
    if not isinstance(route, APIRoute | APIWebSocketRoute):
        return set()
    declared = _resolved_calls(route.dependant, set()) - inherited
    return declared & CALLER_IDENTITY_DEPENDENCIES


class TestRouteAuthCoverage:
    def test_walk_reaches_the_whole_application(self) -> None:
        mounted = _mounted_routes(create_api_app().routes)

        assert len(mounted) >= MINIMUM_MOUNTED_ROUTES

    def test_every_route_declares_a_caller_identity(self) -> None:
        unscoped = [
            _route_key(route, path)
            for path, route, inherited in _mounted_routes(create_api_app().routes)
            if not _self_declared_identity(route, inherited)
        ]

        assert sorted(unscoped) == sorted(ROUTES_WITHOUT_CALLER_IDENTITY), (
            "A route resolves no caller identity of its own. Give it "
            "get_current_organization (or another identity dependency), or add "
            "it to ROUTES_WITHOUT_CALLER_IDENTITY with the reason it is safe."
        )

    def test_allowlist_carries_no_retired_routes(self) -> None:
        routes = _mounted_routes(create_api_app().routes)
        mounted = {_route_key(route, path) for path, route, _ in routes}

        assert set(ROUTES_WITHOUT_CALLER_IDENTITY) <= mounted
