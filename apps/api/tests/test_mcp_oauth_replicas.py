"""MCP OAuth authorization across API replicas.

Each replica is its own ``SibylMcpOAuthProvider`` behind its own ASGI app, as
separate API processes are behind a load balancer. They share nothing but the
auth store, so every step of one authorization may land on any of them.

The embedded engine runs every case; with SIBYL_LIVE_SURREAL_TESTS=1 and a
server SIBYL_SURREAL_URL the same cases run against a server, where each
replica holds its own connection pool and the code-exchange race is decided by
the server's write-conflict detection rather than by one shared connection.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl, AnyUrl, SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route

from sibyl.auth.jwt import verify_access_token
from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider
from sibyl.config import settings
from sibyl.persistence.surreal.auth_runtime import sessions as session_runtime
from sibyl.persistence.surreal.auth_runtime.oauth_authorization import (
    SurrealOAuthAuthorizationStore,
)
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema
from sibyl_core.backends.surreal.records import utcnow
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url

ISSUER = "http://localhost:3334"
REDIRECT_URI = "http://127.0.0.1:9911/callback"
REPLICAS = 4
RACE_REPLICAS = 8
RACE_ROUNDS = 12
CREDENTIALS = {
    "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
    "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
}


def engine_url(engine: str) -> str:
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


async def _drop_namespace(url: str, namespace: str) -> None:
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(url)
    try:
        await client.signin(
            {"username": CREDENTIALS["username"], "password": CREDENTIALS["password"]}
        )
        await client.query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
    finally:
        await client.close()


@pytest.fixture(params=["embedded", "live"])
async def store_clients(request: pytest.FixtureRequest) -> AsyncIterator[list[SurrealAuthClient]]:
    """One auth-store client per replica, all on one namespace.

    A memory:// store lives inside its client, so embedded replicas share one
    client: the store is still the only state they have in common. Against a
    server every replica gets its own client and connection pool.
    """
    url = engine_url(request.param)
    namespace = f"verify_mcp_oauth_{uuid4().hex}"
    count = 1 if request.param == "embedded" else RACE_REPLICAS
    clients = [SurrealAuthClient(url=url, namespace=namespace, **CREDENTIALS) for _ in range(count)]
    await bootstrap_auth_schema(clients[0])
    try:
        yield clients
    finally:
        for client in clients:
            await client.close()
        if request.param == "live":
            with suppress(Exception):
                await _drop_namespace(url, namespace)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("replica-oauth-proof-secret-" * 3))
    monkeypatch.setattr(settings, "server_url", ISSUER)


def _scope_for(client: SurrealAuthClient):
    @asynccontextmanager
    async def scope() -> AsyncIterator[SurrealAuthClient]:
        yield client

    return scope


@dataclass
class Replica:
    provider: SibylMcpOAuthProvider
    http: httpx.AsyncClient
    sessions_created: AsyncMock


def _build_replica(client: SurrealAuthClient, *, user: object, orgs: list[object]) -> Replica:
    provider = SibylMcpOAuthProvider(
        authorization_store=SurrealOAuthAuthorizationStore(_scope_for(client))
    )
    sessions_created = AsyncMock()
    provider._authenticate_local_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    provider._list_user_orgs = AsyncMock(return_value=orgs)  # type: ignore[method-assign]
    provider._create_session_record = sessions_created  # type: ignore[method-assign]
    routes = [
        *create_auth_routes(
            provider,
            issuer_url=AnyHttpUrl(ISSUER),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]
            ),
        ),
        Route("/_oauth/login", provider.ui_login_get, methods=["GET"]),
        Route("/_oauth/login", provider.ui_login_post, methods=["POST"]),
        Route("/_oauth/org", provider.ui_org_get, methods=["GET"]),
        Route("/_oauth/org", provider.ui_org_post, methods=["POST"]),
    ]
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Starlette(routes=routes)), base_url=ISSUER
    )
    return Replica(provider=provider, http=http, sessions_created=sessions_created)


@asynccontextmanager
async def _replicas(
    clients: list[SurrealAuthClient],
    monkeypatch: pytest.MonkeyPatch,
    *,
    count: int,
    orgs: list[object],
) -> AsyncIterator[tuple[list[Replica], SimpleNamespace]]:
    user = SimpleNamespace(id=uuid4())
    # Client registrations already persist through the auth runtime; point it
    # at this store so a client registered on one replica resolves on another.
    monkeypatch.setattr(session_runtime, "_auth_client_scope", _scope_for(clients[0]))
    replicas = [
        _build_replica(clients[index % len(clients)], user=user, orgs=orgs)
        for index in range(count)
    ]
    try:
        yield replicas, user
    finally:
        for replica in replicas:
            await replica.http.aclose()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def _query(location: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


async def test_one_authorization_completes_with_every_step_on_a_different_replica(
    store_clients: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = SimpleNamespace(id=uuid4(), name="Home", is_personal=True)
    team = SimpleNamespace(id=uuid4(), name="Team", is_personal=False)
    async with _replicas(store_clients, monkeypatch, count=REPLICAS, orgs=[home, team]) as (
        replicas,
        user,
    ):
        a, b, c, d = (replica.http for replica in replicas)

        registered = await a.post(
            "/register",
            json={
                "redirect_uris": [REDIRECT_URI],
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "mcp",
                "client_name": "Replica Proof",
            },
        )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        client_secret = registered.json()["client_secret"]
        verifier, challenge = _pkce_pair()

        authorized = await b.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "replica-state",
                "scope": "mcp",
            },
        )
        assert authorized.status_code == 302, authorized.text
        request_id = _query(authorized.headers["location"])["req"]

        login_page = await c.get("/_oauth/login", params={"req": request_id})
        assert login_page.status_code == 200
        assert "Replica Proof" in login_page.text

        logged_in = await d.post(
            "/_oauth/login",
            data={"req": request_id, "email": "proof@example.com", "password": "pw"},
        )
        assert logged_in.status_code == 302
        assert logged_in.headers["location"] == f"/_oauth/org?{urlencode({'req': request_id})}"

        org_page = await a.get("/_oauth/org", params={"req": request_id})
        assert org_page.status_code == 200
        assert "Team" in org_page.text

        chosen = await b.post("/_oauth/org", data={"req": request_id, "org_id": str(team.id)})
        assert chosen.status_code == 302
        assert chosen.headers["location"].startswith(REDIRECT_URI)
        callback = _query(chosen.headers["location"])
        assert callback["state"] == "replica-state"

        token_form = {
            "grant_type": "authorization_code",
            "code": callback["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        issued = await c.post("/token", data=token_form)
        assert issued.status_code == 200, issued.text
        claims = verify_access_token(issued.json()["access_token"])
        assert claims["sub"] == str(user.id)
        assert claims["org"] == str(team.id)

        replayed = await d.post("/token", data=token_form)
        assert replayed.status_code == 400
        assert replayed.json()["error"] == "invalid_grant"

        # The finished flow is closed on every replica, not just the one that ended it.
        for http in (a, b, c, d):
            reused = await http.get("/_oauth/login", params={"req": request_id})
            assert reused.status_code == 400
        assert sum(r.sessions_created.await_count for r in replicas) == 1


def _authorization_params(challenge: str = "challenge") -> AuthorizationParams:
    return AuthorizationParams(
        state="race",
        scopes=["mcp"],
        code_challenge=challenge,
        redirect_uri=AnyUrl(REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
    )


def _form_request(path: str, data: dict[str, str]) -> Request:
    body = urlencode(data).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        },
        receive,
    )


async def _issue_code(replicas: list[Replica], client: OAuthClientInformationFull) -> str:
    url = await replicas[0].provider.authorize(client, _authorization_params())
    request_id = _query(url)["req"]
    response = await replicas[-1].provider.ui_login_post(
        _form_request("/_oauth/login", {"req": request_id, "email": "e", "password": "p"})
    )
    assert response.status_code == 302
    return _query(response.headers["location"])["code"]


async def test_concurrent_exchanges_of_one_code_admit_exactly_one(
    store_clients: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = SimpleNamespace(id=uuid4(), name="Solo", is_personal=True)
    client = OAuthClientInformationFull(client_id="race-client", redirect_uris=[REDIRECT_URI])
    async with _replicas(store_clients, monkeypatch, count=RACE_REPLICAS, orgs=[org]) as (
        replicas,
        _user,
    ):
        for _ in range(RACE_ROUNDS):
            code = await _issue_code(replicas, client)
            # Every replica loads the code before any of them claims it, so the
            # claims collide on the row instead of trailing one another.
            loaded = asyncio.Barrier(len(replicas))

            async def exchange(replica: Replica, code: str = code, loaded=loaded) -> str:
                authorization = await replica.provider.load_authorization_code(client, code)
                await loaded.wait()
                if authorization is None:
                    return "unknown_code"
                try:
                    await replica.provider.exchange_authorization_code(client, authorization)
                except TokenError as exc:
                    return exc.error
                return "tokens"

            outcomes = await asyncio.gather(*(exchange(replica) for replica in replicas))
            assert outcomes.count("tokens") == 1, outcomes
            assert set(outcomes) == {"tokens", "invalid_grant"}, outcomes
        assert sum(r.sessions_created.await_count for r in replicas) == RACE_ROUNDS


async def test_concurrent_logins_for_one_request_issue_one_code(
    store_clients: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = SimpleNamespace(id=uuid4(), name="Solo", is_personal=True)
    client = OAuthClientInformationFull(client_id="login-race", redirect_uris=[REDIRECT_URI])
    async with _replicas(store_clients, monkeypatch, count=RACE_REPLICAS, orgs=[org]) as (
        replicas,
        _user,
    ):
        url = await replicas[0].provider.authorize(client, _authorization_params())
        request_id = _query(url)["req"]
        responses = await asyncio.gather(
            *(
                replica.provider.ui_login_post(
                    _form_request(
                        "/_oauth/login", {"req": request_id, "email": "e", "password": "p"}
                    )
                )
                for replica in replicas
            )
        )
        issued = [response for response in responses if response.status_code == 302]
        assert len(issued) == 1, [response.status_code for response in responses]
        assert {response.status_code for response in responses} == {302, 400}


async def test_expired_requests_and_codes_are_refused_on_read(
    store_clients: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = SimpleNamespace(id=uuid4(), name="Solo", is_personal=True)
    client = OAuthClientInformationFull(client_id="expiry", redirect_uris=[REDIRECT_URI])
    store = SurrealOAuthAuthorizationStore(_scope_for(store_clients[-1]))
    async with _replicas(store_clients, monkeypatch, count=2, orgs=[org]) as (replicas, user):
        past = utcnow() - timedelta(seconds=1)
        await store.create_request(
            request_key="expired-request",
            client_id="expiry",
            state=None,
            scopes=["mcp"],
            code_challenge="challenge",
            redirect_uri=REDIRECT_URI,
            redirect_uri_provided_explicitly=True,
            resource=None,
            expires_at=past,
        )
        page = await replicas[0].http.get("/_oauth/login", params={"req": "expired-request"})
        assert page.status_code == 400

        await store.create_request(
            request_key="short-code",
            client_id="expiry",
            state=None,
            scopes=["mcp"],
            code_challenge="challenge",
            redirect_uri=REDIRECT_URI,
            redirect_uri_provided_explicitly=True,
            resource=None,
            expires_at=utcnow() + timedelta(minutes=1),
        )
        issued = await store.issue_code(
            "short-code",
            code="already-expired-code",
            user_id=user.id,
            organization_id=org.id,
            code_expires_at=past,
            require_authenticated=False,
        )
        assert issued is not None
        for replica in replicas:
            assert (
                await replica.provider.load_authorization_code(client, "already-expired-code")
                is None
            )
        assert await store.consume_code("already-expired-code") is None

        # Creating a request sweeps expired rows; reads never depended on it.
        await store.create_request(
            request_key="sweeper",
            client_id="expiry",
            state=None,
            scopes=None,
            code_challenge="challenge",
            redirect_uri=REDIRECT_URI,
            redirect_uri_provided_explicitly=True,
            resource=None,
            expires_at=utcnow() + timedelta(minutes=1),
        )
        rows = await store_clients[0].execute_query(
            "SELECT count() AS count FROM oauth_authorization_requests GROUP ALL;"
        )
        assert rows == [{"count": 1}]


async def test_codes_and_request_keys_are_stored_only_as_hashes(
    store_clients: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    org = SimpleNamespace(id=uuid4(), name="Solo", is_personal=True)
    client = OAuthClientInformationFull(client_id="hashes", redirect_uris=[REDIRECT_URI])
    async with _replicas(store_clients, monkeypatch, count=2, orgs=[org]) as (replicas, _user):
        url = await replicas[0].provider.authorize(client, _authorization_params())
        request_id = _query(url)["req"]
        response = await replicas[1].provider.ui_login_post(
            _form_request("/_oauth/login", {"req": request_id, "email": "e", "password": "p"})
        )
        code = _query(response.headers["location"])["code"]
        rows = await store_clients[0].execute_query("SELECT * FROM oauth_authorization_requests;")
        stored = repr(rows)
        assert request_id not in stored
        assert code not in stored
        assert hashlib.sha256(code.encode()).hexdigest() in stored
