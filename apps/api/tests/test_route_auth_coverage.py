"""Every mounted route must receive the caller identity it needs.

Scope tests elsewhere in this suite override the auth dependencies, so a route
that never declared them looks exactly like a route that did. This test reads
the mounted application instead of calling it, which is the only way a missing
dependency becomes visible.

Two things make the rule stricter than "the route is authenticated", and both
are load-bearing.

A router-level gate such as require_org_admin() proves the caller administers
some organization, and every user owns a personal organization with the OWNER
role, so inheriting that gate says nothing about whose data the handler is
about to serve. A route sitting under one of those gates therefore has to
receive an organization, not merely sit behind a check.

And a dependency only counts when its value reaches something that uses it. A
Depends() listed in a route or router decorator has nowhere to put its result,
so it can prove a check ran but can never hand the handler an organization.
Only parameter-bound dependencies are credited, which is why this reads the
route's own parameters rather than the whole resolved dependency tree.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Host, Mount

from sibyl.api.app import create_api_app
from sibyl.api.dependencies import get_graph_store, get_knowledge_read_service
from sibyl.auth.dependencies import (
    get_auth_context,
    get_current_org_role,
    get_current_organization,
    get_current_user,
    require_org_admin,
    require_org_role,
)
from sibyl.persistence.operations_runtime import (
    require_global_admin,
    require_setup_mode_or_admin,
    require_setup_mode_or_auth,
)

# Dependencies that resolve an organization and hand back something already
# scoped to it, so a handler receiving one cannot read across tenants.
# test_org_scoped_providers_resolve_an_organization holds them to that claim.
ORG_SCOPED_PROVIDERS = frozenset({get_graph_store, get_knowledge_read_service})

# Values that tell a handler which organization it is answering for.
ORG_BEARING_DEPENDENCIES = (
    frozenset({get_auth_context, get_current_organization}) | ORG_SCOPED_PROVIDERS
)

# Values that name the caller, whether or not they carry an organization.
CALLER_IDENTITY_DEPENDENCIES = ORG_BEARING_DEPENDENCIES | frozenset(
    {get_current_org_role, get_current_user}
)

# Gates that raise by themselves, so a route may discard what they return.
# Org role gates are deliberately absent: they authorize a caller against an
# organization someone else resolved, which is the vacuity this test exists to
# catch, so they are listed as gate factories instead.
ENFORCING_GATES = frozenset(
    {require_global_admin, require_setup_mode_or_admin, require_setup_mode_or_auth}
)

ORG_ROLE_GATE_FACTORIES = (require_org_admin, require_org_role)

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


def _is_org_role_gate(call: Any) -> bool:
    qualname = getattr(call, "__qualname__", "")
    return any(
        qualname.startswith(f"{factory.__qualname__}.<locals>.")
        for factory in ORG_ROLE_GATE_FACTORIES
    )


def _route_key(route: Any, path: str) -> str:
    methods = getattr(route, "methods", None)
    label = ",".join(sorted(methods)) if methods else "WS"
    return f"{label} {path}"


def _mounted_routes(routes: list[Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten the app into its leaf routes."""
    flattened: list[tuple[str, Any]] = []
    for route in routes:
        # FastAPI mounts an included router as a single node rather than copying
        # its routes, so the tree has to be walked through that node.
        include = getattr(route, "include_context", None)
        if include is not None:
            flattened += _mounted_routes(include.included_router.routes, prefix + include.prefix)
            continue

        flattened.append((prefix + getattr(route, "path", ""), route))
    return flattened


def _received(route: Any) -> set[Any]:
    """Dependencies whose resolved value the route is actually handed.

    A Depends() in a route or router decorator resolves into nowhere, so it
    never appears here however deeply it reaches an identity internally.
    """
    if not isinstance(route, APIRoute | APIWebSocketRoute):
        return set()
    return {sub.call for sub in route.dependant.dependencies if sub.name is not None and sub.call}


def _declared(route: Any) -> set[Any]:
    if not isinstance(route, APIRoute | APIWebSocketRoute):
        return set()
    return {sub.call for sub in route.dependant.dependencies if sub.call}


def _has_caller_identity(route: Any) -> bool:
    declared = _declared(route)
    received = _received(route)

    if any(_is_org_role_gate(call) for call in declared):
        return bool(received & ORG_BEARING_DEPENDENCIES)
    return bool(received & CALLER_IDENTITY_DEPENDENCIES) or bool(declared & ENFORCING_GATES)


def _unrecognized_gate() -> Any:
    """A role gate this test has never heard of, shaped like the real ones."""

    async def _check(ctx: Any = Depends(get_auth_context)) -> None:
        return None

    return _check


class TestRouteAuthCoverage:
    def test_walk_reaches_the_whole_application(self) -> None:
        assert len(_mounted_routes(create_api_app().routes)) >= MINIMUM_MOUNTED_ROUTES

    def test_every_route_receives_a_caller_identity(self) -> None:
        unscoped = [
            _route_key(route, path)
            for path, route in _mounted_routes(create_api_app().routes)
            if not _has_caller_identity(route)
        ]

        assert sorted(unscoped) == sorted(ROUTES_WITHOUT_CALLER_IDENTITY), (
            "A route is not handed the caller identity it needs. Bind "
            "get_current_organization (or another org-bearing dependency) to a "
            "parameter, or add the route to ROUTES_WITHOUT_CALLER_IDENTITY with "
            "the reason it is safe to answer without one."
        )

    def test_org_scoped_providers_resolve_an_organization(self) -> None:
        """The providers are trusted to carry an org, so prove that they do."""
        for provider in ORG_SCOPED_PROVIDERS:
            dependant = get_dependant(path="/", call=provider)
            reached: set[Any] = set()
            queue = list(dependant.dependencies)
            while queue:
                sub = queue.pop()
                if sub.name is None or sub.call is None:
                    continue
                reached.add(sub.call)
                queue += sub.dependencies

            assert get_current_organization in reached, provider

    def test_a_discarded_identity_does_not_scope_a_route(self) -> None:
        """Depends() in the decorator resolves into nowhere, so it proves nothing."""
        router = APIRouter(dependencies=[Depends(require_org_admin())])

        @router.get("/discarded", dependencies=[Depends(get_current_user)])
        async def discarded() -> dict[str, str]:
            return {}

        assert not _has_caller_identity(self._only_route(router, "/discarded"))

    def test_an_unrecognized_role_gate_does_not_scope_a_route(self) -> None:
        """A gate reaching get_auth_context internally still hands over nothing."""
        router = APIRouter()

        @router.get("/unknown-gate", dependencies=[Depends(_unrecognized_gate())])
        async def unknown_gate() -> dict[str, str]:
            return {}

        assert not _has_caller_identity(self._only_route(router, "/unknown-gate"))

    def test_a_bound_organization_scopes_a_route(self) -> None:
        """The negative cases would be vacuous if nothing satisfied the rule."""
        router = APIRouter(dependencies=[Depends(require_org_admin())])

        @router.get("/bound")
        async def bound(org: Any = Depends(get_current_organization)) -> dict[str, str]:
            return {}

        assert _has_caller_identity(self._only_route(router, "/bound"))

    def test_no_sub_application_hides_routes_from_the_walk(self) -> None:
        mounted = _mounted_routes(create_api_app().routes)
        opaque = [_route_key(r, p) for p, r in mounted if isinstance(r, Mount | Host)]

        assert opaque == [], (
            "A mounted sub-application is a single opaque leaf to this walk, so "
            "allowlisting it would exempt every route inside it. Teach "
            "_mounted_routes to descend the sub-application instead."
        )

    def test_allowlist_carries_no_retired_routes(self) -> None:
        mounted = {
            _route_key(route, path) for path, route in _mounted_routes(create_api_app().routes)
        }

        assert set(ROUTES_WITHOUT_CALLER_IDENTITY) <= mounted

    @staticmethod
    def _only_route(router: APIRouter, path: str) -> Any:
        app = FastAPI()
        app.include_router(router)
        matches = [route for route_path, route in _mounted_routes(app.routes) if route_path == path]

        assert len(matches) == 1
        return matches[0]
