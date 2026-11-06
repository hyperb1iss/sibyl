"""Who may do what in sibylnet: organization role x credential x project role.

Read ``expected()`` as the spec. Every cell is derived there from the rules the
guide (docs/guide/sibylnet.md) and the route docstrings state:

- Every organization role reads every channel; posting needs a write role
  (owner, admin or member).
- Naming a project takes contributor access to it, within the projects the
  API key allows. Organization owners and admins hold owner access to every
  project, the repository-wide project authorization rule.
- A decision is remembered first, with ``remember``'s scope rules: the
  project's default scope (project P defaults to project scope) when the
  writer may write project memory there, private otherwise, and a key held to
  memory spaces writes only those spaces.
- Distillation reflects an author's lines as them, under the key they posted
  with as it stands now: into the pinned project when they may contribute
  there, into their private memory otherwise, and an author who can write
  neither is skipped and recorded.

One world is built per engine: one user per (role, credential, project role)
in one organization, each holding exactly that grant. Every principal posts
one line into a thread pinned to P, the distillation job runs once, and then
each principal runs every operation. Each test below asserts one cell.

Credentials:
- ``session``: a signed-in session, no API key.
- ``key``: an API key with no restriction.
- ``key@P`` / ``key@Q``: a key held to project P, or to another project Q.
- ``key#P`` / ``key#me``: a key held to P's project memory space, or to the
  principal's own private memory space.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from pydantic import SecretStr

from sibyl.api.routes import net as net_routes
from sibyl.auth.dependencies import _api_key_claims
from sibyl.auth.jwt import create_access_token, verify_access_token
from sibyl.jobs import net as jobs_net
from sibyl.jobs.net import distill_idle_net_threads
from sibyl.persistence.surreal import content as surreal_content, organization_runtime
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver
from sibyl.persistence.surreal.auth_runtime import _common as auth_common
from sibyl.persistence.surreal.auth_runtime.api_keys import (
    authenticate_api_key,
    create_api_key_for_user,
)
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
)
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.config import settings
from sibyl_core.services import content_client, graph_client
from sibyl_core.services.graph import SurrealGraphClient
from sibyl_core.services.net import list_net_threads, post_net_message
from tests.test_routes_net import CREDENTIALS as SURREAL_LOGIN, engine_url

pytestmark = pytest.mark.asyncio(loop_scope="module")

ROLES = ("owner", "admin", "member", "viewer")
CREDENTIAL_KINDS = ("session", "key", "key@P", "key@Q", "key#P", "key#me")
PROJECT_ROLES = ("none", "viewer", "contributor")
OPERATIONS = (
    "post",
    "post_project",
    "pin",
    "decision",
    "decision_unscoped",
    "feed",
    "channels",
    "threads",
    "distill",
)

WRITE_ROLES = {"owner", "admin", "member"}
ORG_ADMINS = {"owner", "admin"}
# Memory a credential may write, as the memory policy reads it; None is no
# restriction.
MEMORY_GRANTS: dict[str, frozenset[str] | None] = {
    "session": None,
    "key": None,
    "key@P": None,
    "key@Q": None,
    "key#P": frozenset({"project:P"}),
    "key#me": frozenset({"private"}),
}
DISTILL_CHANNEL = "#matrix"
DISTILL_THREAD = "distill"


@dataclass(frozen=True, slots=True)
class Principal:
    role: str
    credential: str
    project_role: str

    @property
    def label(self) -> str:
        return f"{self.role}-{self.credential}-{self.project_role}"


PRINCIPALS = [
    Principal(role, credential, project_role)
    for role in ROLES
    for credential in CREDENTIAL_KINDS
    for project_role in PROJECT_ROLES
]


def expected(principal: Principal, operation: str) -> str:
    """The documented outcome of one cell."""
    writes = principal.role in WRITE_ROLES
    contributes = principal.role in ORG_ADMINS or principal.project_role == "contributor"
    key_reaches_p = principal.credential != "key@Q"
    grants = MEMORY_GRANTS[principal.credential]
    project_memory = grants is None or "project:P" in grants
    private_memory = grants is None or "private" in grants
    may_name_p = writes and contributes and key_reaches_p

    if operation in {"feed", "channels", "threads"}:
        return "200"
    if operation == "post":
        return "201" if writes else "403"
    if operation == "post_project":
        return "201" if may_name_p else "403"
    if operation == "pin":
        return "201 pinned:P" if may_name_p else "403"
    if operation == "decision":
        if not may_name_p:
            return "403"
        # P defaults to project scope; a credential that may not write project
        # memory there falls back to private, as remember does.
        return "201 memory:project:P" if project_memory else "201 memory:private:me"
    if operation == "decision_unscoped":
        if not writes:
            return "403"
        return "201 memory:private:me" if private_memory else "403"
    if operation == "distill":
        if not writes:
            return "skipped:principal_cannot_write"
        if contributes and key_reaches_p and project_memory:
            return "project:P"
        if private_memory:
            return "private:me"
        return "skipped:no_writable_scope"
    raise AssertionError(operation)


@dataclass
class World:
    org_id: str
    slug: str
    project_p: str
    project_q: str
    seeder: str
    users: dict[Principal, str] = field(default_factory=dict)
    tokens: dict[Principal, str] = field(default_factory=dict)
    key_ids: dict[Principal, str] = field(default_factory=dict)
    observed: dict[tuple[Principal, str], str] = field(default_factory=dict)
    distill_summary: dict[str, int] = field(default_factory=dict)
    distill_thread: object | None = None


@asynccontextmanager
async def isolated_stores(engine: str) -> AsyncIterator[SimpleNamespace]:
    """Auth, content and graph stores in a namespace of their own, plus a net app."""
    url = engine_url(engine)
    namespace = f"verify_net_matrix_{uuid4().hex}"
    auth = SurrealAuthClient(url=url, namespace=namespace, **SURREAL_LOGIN)
    content = SurrealContentClient(url=url, namespace=namespace, **SURREAL_LOGIN)
    patch = pytest.MonkeyPatch()
    graphs: dict[str, SurrealGraphClient] = {}
    try:
        patch.setattr(settings, "surreal_url", url)
        patch.setattr(settings, "jwt_secret", SecretStr("isolated-sibylnet-matrix-proof-" * 3))
        patch.setattr(
            "sibyl_core.services.content_models.configured_raw_memory_embedding_provider",
            lambda: None,
        )
        await bootstrap_auth_schema(auth)
        await bootstrap_content_schema(content)

        @asynccontextmanager
        async def auth_scope():
            yield auth

        async def content_scope():
            return content

        def new_graph_client(group: str) -> SurrealGraphClient:
            return graphs.setdefault(
                group,
                SurrealGraphClient(
                    group_id=group, url=url, namespace_prefix=f"{namespace}_", **SURREAL_LOGIN
                ),
            )

        patch.setattr(auth_common, "surreal_auth_client_scope", auth_scope)
        patch.setattr(organization_runtime, "surreal_auth_client_scope", auth_scope)
        patch.setattr(content_client, "get_shared_surreal_content_client", content_scope)
        patch.setattr(surreal_content, "get_shared_surreal_content_client", content_scope)
        patch.setattr(graph_client, "_new_graph_client", new_graph_client)
        resolver = SurrealAuthContextResolver.from_client(auth)

        app = FastAPI()

        @app.middleware("http")
        async def fixture_identity(request: Request, call_next):
            token = request.headers["Authorization"].removeprefix("Bearer ")
            if token.startswith("sk_"):
                key = await authenticate_api_key(token)
                assert key is not None
                claims = _api_key_claims(key, scopes=list(key.scopes))
            else:
                claims = verify_access_token(token)
            request.state.auth_context = await resolver.resolve(claims)
            return await call_next(request)

        app.include_router(net_routes.router, prefix="/api")
        yield SimpleNamespace(auth=auth, content=content, app=app, patch=patch)
    finally:
        for group in graphs:
            graph_client._clients.pop(group, None)
            graph_client.mark_graph_schema_dirty(group)
        if engine == "live":
            for store in (auth, *graphs.values()):
                await store.execute_query(f"REMOVE NAMESPACE IF EXISTS {store.namespace};")
        for store in graphs.values():
            await store.close()
        await content.close()
        await auth.close()
        patch.undo()


async def seed(stores: SimpleNamespace) -> World:
    org_id = str(uuid4())
    world = World(
        org_id=org_id,
        slug=f"matrix-{org_id[:8]}",
        project_p=f"project_p_{uuid4().hex[:10]}",
        project_q=f"project_q_{uuid4().hex[:10]}",
        seeder=str(uuid4()),
    )
    p_record, q_record = str(uuid4()), str(uuid4())
    p_space = str(uuid4())
    rows: dict[str, list[dict[str, object]]] = {
        "organizations": [{"uuid": org_id, "name": "Matrix", "slug": world.slug}],
        "users": [
            {"uuid": world.seeder, "email": f"seeder+{org_id[:8]}@example.test", "name": "Seeder"}
        ],
        "organization_members": [
            {
                "uuid": str(uuid4()),
                "organization_id": org_id,
                "user_id": world.seeder,
                "role": "owner",
            }
        ],
        "projects": [
            {
                "uuid": p_record,
                "organization_id": org_id,
                "name": world.project_p,
                "slug": world.project_p,
                "graph_project_id": world.project_p,
                "visibility": "private",
                "default_memory_scope": "project",
            },
            {
                "uuid": q_record,
                "organization_id": org_id,
                "name": world.project_q,
                "slug": world.project_q,
                "graph_project_id": world.project_q,
                "visibility": "private",
            },
        ],
        "project_members": [],
        "memory_spaces": [
            {
                "uuid": p_space,
                "organization_id": org_id,
                "memory_scope": "project",
                "scope_key": world.project_p,
                "created_by_user_id": world.seeder,
            }
        ],
    }
    private_spaces: dict[Principal, str] = {}
    for index, principal in enumerate(PRINCIPALS):
        user = str(uuid4())
        world.users[principal] = user
        rows["users"].append(
            {
                "uuid": user,
                "email": f"u{index}+{org_id[:8]}@example.test",
                "name": f"User {principal.label}",
            }
        )
        rows["organization_members"].append(
            {
                "uuid": str(uuid4()),
                "organization_id": org_id,
                "user_id": user,
                "role": principal.role,
            }
        )
        if principal.project_role != "none":
            rows["project_members"].append(
                {
                    "uuid": str(uuid4()),
                    "organization_id": org_id,
                    "project_id": p_record,
                    "user_id": user,
                    "role": f"project_{principal.project_role}",
                }
            )
        if principal.credential == "key#me":
            private_spaces[principal] = str(uuid4())
            rows["memory_spaces"].append(
                {
                    "uuid": private_spaces[principal],
                    "organization_id": org_id,
                    "memory_scope": "private",
                    "scope_key": user,
                    "created_by_user_id": user,
                }
            )
    for table, records in rows.items():
        for record in records:
            await stores.auth.execute_query(f"CREATE {table} CONTENT $record;", record=record)

    for principal, user in world.users.items():
        if principal.credential == "session":
            world.tokens[principal] = create_access_token(
                user_id=UUID(user), organization_id=UUID(org_id)
            )
            continue
        projects = {"key@P": [world.project_p], "key@Q": [world.project_q]}.get(
            principal.credential
        )
        spaces = {"key#P": [UUID(p_space)]}.get(principal.credential)
        if principal.credential == "key#me":
            spaces = [UUID(private_spaces[principal])]
        key, raw = await create_api_key_for_user(
            organization_id=UUID(org_id),
            user_id=UUID(user),
            name=principal.label,
            live=False,
            scopes=["api:read", "api:write"],
            project_ids=projects,
            memory_space_ids=spaces,
            expires_at=None,
            request=None,
        )
        world.tokens[principal] = raw
        world.key_ids[principal] = str(key.id)
    return world


@asynccontextmanager
async def http_as(stores: SimpleNamespace, token: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stores.app),
        base_url="http://net.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    ) as client:
        yield client


async def distill_everyone(stores: SimpleNamespace, world: World) -> None:
    """Each principal's own line in one thread pinned to P, distilled in one pass."""
    seeder_token = create_access_token(
        user_id=UUID(world.seeder), organization_id=UUID(world.org_id)
    )
    async with http_as(stores, seeder_token) as http:
        pinned = await http.post(
            "/api/net/messages",
            json={
                "channel": DISTILL_CHANNEL,
                "thread": DISTILL_THREAD,
                "body": "Thread for the matrix.",
                "project_id": world.project_p,
            },
        )
        assert pinned.status_code == 201, pinned.text
    for principal, user in world.users.items():
        body = f"Line from {principal.label}: decided to keep the cache."
        if principal.role in WRITE_ROLES:
            async with http_as(stores, world.tokens[principal]) as http:
                posted = await http.post(
                    "/api/net/messages",
                    json={"channel": DISTILL_CHANNEL, "thread": DISTILL_THREAD, "body": body},
                )
            assert posted.status_code == 201, (principal.label, posted.text)
        else:
            # A viewer cannot post; this is a line they wrote before they were
            # made a viewer, stored as the route would have stored it.
            await post_net_message(
                organization_id=world.org_id,
                channel=DISTILL_CHANNEL,
                thread=DISTILL_THREAD,
                kind="message",
                body=body,
                author_user_id=user,
                api_key_id=world.key_ids.get(principal),
            )
    await stores.content.execute_query(
        "UPDATE net_messages SET created_at = created_at - 45m WHERE organization_id = $org;"
        "UPDATE net_threads SET last_message_at = last_message_at - 45m, "
        "first_message_at = first_message_at - 45m WHERE organization_id = $org;",
        org=world.org_id,
    )

    calls: list[SimpleNamespace] = []
    real = jobs_net.reflect_memory

    async def recorded(content: str, **kwargs: object):
        pack = await real(content, **kwargs)  # type: ignore[arg-type]
        calls.append(SimpleNamespace(kwargs=kwargs, pack=pack, transcript=content))
        return pack

    stores.patch.setattr(jobs_net, "reflect_memory", recorded)
    try:
        world.distill_summary = await distill_idle_net_threads({})
    finally:
        stores.patch.setattr(jobs_net, "reflect_memory", real)

    states = await list_net_threads(organization_id=world.org_id, channel=DISTILL_CHANNEL)
    world.distill_thread = next(state for state in states if state.thread == DISTILL_THREAD)
    skipped = {}
    for entry in world.distill_thread.reflection_skipped:
        user, _, reason = entry.partition(": ")
        skipped[user] = reason
    for principal, user in world.users.items():
        if user in skipped:
            world.observed[(principal, "distill")] = f"skipped:{skipped[user]}"
            continue
        mine = [call for call in calls if call.kwargs.get("principal_id") == user]
        if len(mine) != 1 or not mine[0].pack.source_id:
            world.observed[(principal, "distill")] = f"reflected {len(mine)} times"
            continue
        world.observed[(principal, "distill")] = destination(
            world, user, mine[0].kwargs.get("memory_scope"), mine[0].kwargs.get("scope_key")
        )
        if f"Line from {principal.label}:" not in mine[0].transcript:
            world.observed[(principal, "distill")] += " (without their own line)"
        if mine[0].transcript.count("Line from ") != 1:
            world.observed[(principal, "distill")] += " (with someone else's line)"


def project_label(world: World, project_id: object) -> str:
    return {world.project_p: "P", world.project_q: "Q"}.get(str(project_id), str(project_id))


def destination(world: World, user: str, scope: object, key: object) -> str:
    if scope == "project":
        return f"project:{project_label(world, key)}"
    if scope == "private" and key in (None, "", user):
        return "private:me"
    return f"{scope}:{key}"


async def run_operations(
    stores: SimpleNamespace, world: World, index: int, principal: Principal
) -> None:
    user = world.users[principal]
    channel = f"#cell-{index}"
    record = world.observed

    async def memory_of(response: httpx.Response) -> str:
        memory_id = response.json().get("decision_memory_id")
        rows = content_client.normalize_records(
            await stores.content.execute_query(
                "SELECT memory_scope, scope_key FROM raw_captures WHERE uuid = $id;", id=memory_id
            )
        )
        if not rows:
            return " memory:none"
        return " memory:" + destination(
            world, user, rows[0].get("memory_scope"), rows[0].get("scope_key")
        )

    async with http_as(stores, world.tokens[principal]) as http:

        async def send(**body: object) -> httpx.Response:
            return await http.post("/api/net/messages", json={"channel": channel, **body})

        response = await send(body="plain")
        record[(principal, "post")] = str(response.status_code)

        response = await send(body="names P", project_id=world.project_p)
        record[(principal, "post_project")] = str(response.status_code)

        response = await send(thread="pin", body="pins P", project_id=world.project_p)
        outcome = str(response.status_code)
        if response.status_code == 201:
            states = await list_net_threads(organization_id=world.org_id, channel=channel)
            pin = next((state.project_id for state in states if state.thread == "pin"), None)
            outcome += f" pinned:{project_label(world, pin)}"
        record[(principal, "pin")] = outcome

        response = await send(
            thread="decide", kind="decision", body="Decided: keep P", project_id=world.project_p
        )
        outcome = str(response.status_code)
        if response.status_code == 201:
            outcome += await memory_of(response)
        record[(principal, "decision")] = outcome

        response = await send(kind="decision", body="Decided: unscoped")
        outcome = str(response.status_code)
        if response.status_code == 201:
            outcome += await memory_of(response)
        record[(principal, "decision_unscoped")] = outcome

        response = await http.get("/api/net/feed", params={"limit": 5})
        record[(principal, "feed")] = str(response.status_code)
        response = await http.get("/api/net/channels")
        record[(principal, "channels")] = str(response.status_code)
        response = await http.get("/api/net/threads", params={"channel": DISTILL_CHANNEL})
        record[(principal, "threads")] = str(response.status_code)


@pytest_asyncio.fixture(scope="module", loop_scope="module", params=["embedded", "live"])
async def world(request: pytest.FixtureRequest) -> AsyncIterator[World]:
    async with isolated_stores(request.param) as stores:
        built = await seed(stores)
        await distill_everyone(stores, built)
        for index, principal in enumerate(PRINCIPALS):
            await run_operations(stores, built, index, principal)
        yield built


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("principal", PRINCIPALS, ids=[p.label for p in PRINCIPALS])
async def test_cell(world: World, principal: Principal, operation: str) -> None:
    assert world.observed[(principal, operation)] == expected(principal, operation)


async def test_the_matrix_thread_settles_with_every_author_accounted_for(world: World) -> None:
    thread = world.distill_thread
    assert thread is not None
    assert world.distill_summary["failed"] == 0, world.distill_summary
    assert thread.reflection_error is None
    assert thread.reflected_through == thread.last_message_at
    skipped = sum(
        1 for principal in PRINCIPALS if expected(principal, "distill").startswith("skipped:")
    )
    assert world.distill_summary["authors_skipped"] == skipped
    assert thread.message_count == len(PRINCIPALS) + 1
