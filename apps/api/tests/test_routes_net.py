"""sibylnet over HTTP: posting, the settled feed, decisions and the listings.

Auth, content and graph run on the embedded engine with persisted members,
projects and grants, and on a live server too when SIBYL_LIVE_SURREAL_TESTS=1
names one; identity comes from signed tokens resolved by the native resolver.
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import SecretStr

from sibyl.api.net_events import net_wakeup_subscriber
from sibyl.api.pubsub import get_pubsub
from sibyl.api.routes import net as net_routes
from sibyl.auth.dependencies import _api_key_claims
from sibyl.auth.jwt import create_access_token, verify_access_token
from sibyl.persistence.surreal import content as surreal_content, organization_runtime
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver
from sibyl.persistence.surreal.auth_runtime import _common as auth_common
from sibyl.persistence.surreal.auth_runtime.api_keys import (
    authenticate_api_key,
    create_api_key_for_user,
    revoke_api_key_for_user,
)
from sibyl_core.auth import OrganizationRole
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
)
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.config import settings
from sibyl_core.services import content_client, graph_client
from sibyl_core.services.graph import SurrealGraphClient
from sibyl_core.services.net import SETTLE_SECONDS, NetCursor

CREDENTIALS = {
    "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
    "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
}


def engine_url(engine: str) -> str:
    """The embedded engine, or a live server when SIBYL_LIVE_SURREAL_TESTS=1 names one."""
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


def _project_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


# A live server takes about a minute to migrate fresh auth and content
# namespaces, so the live run migrates them once and isolates each test by its
# own organization and users; the embedded engine gets fresh stores every time.
_LIVE = {"namespace": f"verify_net_routes_{uuid4().hex}", "migrated": False}


@pytest.fixture(scope="session")
def live_namespace() -> Iterator[str]:
    yield str(_LIVE["namespace"])
    if not _LIVE["migrated"]:
        return

    async def remove() -> None:
        client = SurrealAuthClient(
            url=os.environ["SIBYL_SURREAL_URL"], namespace=str(_LIVE["namespace"]), **CREDENTIALS
        )
        try:
            await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        finally:
            await client.close()

    asyncio.run(remove())


@pytest.fixture(params=["embedded", "live"])
async def org(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SimpleNamespace]:
    url = engine_url(request.param)
    live = request.param == "live"
    namespace = (
        request.getfixturevalue("live_namespace") if live else f"verify_net_routes_{uuid4().hex}"
    )
    auth = SurrealAuthClient(url=url, namespace=namespace, **CREDENTIALS)
    content = SurrealContentClient(url=url, namespace=namespace, **CREDENTIALS)
    monkeypatch.setattr(settings, "surreal_url", url)
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("isolated-sibylnet-route-proof-" * 3))
    if not (live and _LIVE["migrated"]):
        await bootstrap_auth_schema(auth)
        await bootstrap_content_schema(content)
        _LIVE["migrated"] = _LIVE["migrated"] or live

    @asynccontextmanager
    async def auth_scope():
        yield auth

    async def content_scope():
        return content

    monkeypatch.setattr(auth_common, "surreal_auth_client_scope", auth_scope)
    monkeypatch.setattr(organization_runtime, "surreal_auth_client_scope", auth_scope)
    monkeypatch.setattr(content_client, "get_shared_surreal_content_client", content_scope)
    monkeypatch.setattr(surreal_content, "get_shared_surreal_content_client", content_scope)
    graphs: dict[str, SurrealGraphClient] = {}

    def new_graph_client(group: str) -> SurrealGraphClient:
        return graphs.setdefault(
            group,
            SurrealGraphClient(
                group_id=group, url=url, namespace_prefix=f"{namespace}_", **CREDENTIALS
            ),
        )

    monkeypatch.setattr(graph_client, "_new_graph_client", new_graph_client)
    resolver = SurrealAuthContextResolver.from_client(auth)

    app = FastAPI()

    @app.middleware("http")
    async def fixture_identity(request: Request, call_next):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if token.startswith("sk_"):
            # An API key resolves to claims exactly as request auth builds them.
            key = await authenticate_api_key(token)
            assert key is not None
            claims = _api_key_claims(key, scopes=list(key.scopes))
        else:
            claims = verify_access_token(token)
        request.state.auth_context = await resolver.resolve(claims)
        return await call_next(request)

    app.include_router(net_routes.router, prefix="/api")
    # The server subscribes this at startup; posts wake long-polls through it.
    await get_pubsub().subscribe(net_wakeup_subscriber)
    runtime = SimpleNamespace(
        auth=auth,
        content=content,
        app=app,
        id=str(uuid4()),
        other_org=str(uuid4()),
        ada=str(uuid4()),
        grace=str(uuid4()),
        vic=str(uuid4()),
        ozzy=str(uuid4()),
        owen=str(uuid4()),
        shared=_project_id("project"),
        other=_project_id("project"),
        readonly=_project_id("project"),
        secret=_project_id("project"),
    )
    runtime.slug = f"acme-{runtime.id[:8]}"
    await seed_auth(runtime)
    try:
        yield runtime
    finally:
        # The live namespace outlives the test; leave nothing for the next
        # one's cross-organization distillation pass to find.
        await content.execute_query(
            "DELETE net_messages WHERE organization_id IN $orgs; "
            "DELETE net_threads WHERE organization_id IN $orgs; "
            "DELETE raw_captures WHERE organization_id IN $orgs;",
            orgs=[runtime.id, runtime.other_org],
        )
        for group in graphs:
            graph_client._clients.pop(group, None)
            graph_client.mark_graph_schema_dirty(group)
        if live:
            for client in graphs.values():
                await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        for client in graphs.values():
            await client.close()
        await content.close()
        await auth.close()


async def seed_auth(org: SimpleNamespace) -> None:
    names = {
        org.ada: "Ada",
        org.grace: "Grace",
        org.vic: "Vic",
        org.ozzy: "Ozzy",
        org.owen: "Owen",
    }
    project_records = {
        org.shared: str(uuid4()),
        org.other: str(uuid4()),
        org.readonly: str(uuid4()),
        org.secret: str(uuid4()),
    }
    records = {
        "users": [
            {"uuid": user, "email": email(org, name), "name": name} for user, name in names.items()
        ],
        "organizations": [
            {"uuid": org.id, "name": "Acme", "slug": org.slug},
            {"uuid": org.other_org, "name": "Elsewhere", "slug": f"else-{org.other_org[:8]}"},
        ],
        "organization_members": [
            {"uuid": str(uuid4()), "organization_id": organization, "user_id": user, "role": role}
            for organization, user, role in (
                (org.id, org.ada, "member"),
                (org.id, org.grace, "member"),
                (org.id, org.vic, "viewer"),
                (org.id, org.owen, "owner"),
                (org.other_org, org.ozzy, "member"),
            )
        ],
        "projects": [
            {
                "uuid": record_id,
                "organization_id": org.id,
                "name": graph_id,
                "slug": graph_id,
                "graph_project_id": graph_id,
                "visibility": "private",
                **({"default_memory_scope": "project"} if graph_id == org.shared else {}),
            }
            for graph_id, record_id in project_records.items()
        ],
        "project_members": [
            {
                "uuid": str(uuid4()),
                "organization_id": org.id,
                "project_id": project_records[graph_id],
                "user_id": user,
                "role": role,
            }
            for graph_id, user, role in (
                (org.shared, org.ada, "project_contributor"),
                (org.shared, org.grace, "project_contributor"),
                (org.other, org.ada, "project_contributor"),
                (org.other, org.grace, "project_contributor"),
                (org.readonly, org.ada, "project_viewer"),
                (org.secret, org.grace, "project_contributor"),
            )
        ],
    }
    for table, rows in records.items():
        for record in rows:
            await org.auth.execute_query(f"CREATE {table} CONTENT $record;", record=record)


def email(org: SimpleNamespace, name: str) -> str:
    # Unique per test: the live namespace is shared and emails are a unique index.
    return f"{name.lower()}+{org.id[:8]}@example.test"


def token_for(org: SimpleNamespace, user: str, organization: str | None = None) -> str:
    return create_access_token(user_id=UUID(user), organization_id=UUID(organization or org.id))


@asynccontextmanager
async def client_for(
    org: SimpleNamespace,
    user: str,
    organization: str | None = None,
    *,
    token: str | None = None,
):
    transport = httpx.ASGITransport(app=org.app)
    bearer = token or token_for(org, user, organization)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://net.test",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=30,
    ) as client:
        yield client


async def post(
    org: SimpleNamespace,
    user: str,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    **body: object,
) -> httpx.Response:
    async with client_for(org, user, token=token) as client:
        return await client.post("/api/net/messages", json=body, headers=headers)


async def api_key(
    org: SimpleNamespace, user: str, *, projects: list[str] | None
) -> tuple[str, str]:
    """A real key for ``user``, held to ``projects`` when given; returns (id, secret)."""
    key, raw = await create_api_key_for_user(
        organization_id=UUID(org.id),
        user_id=UUID(user),
        name="agent",
        live=False,
        scopes=["api:read", "api:write"],
        project_ids=projects,
        expires_at=None,
        request=None,
    )
    return str(key.id), raw


async def revoke_key(org: SimpleNamespace, user: str, key_id: str) -> None:
    await revoke_api_key_for_user(
        api_key_id=UUID(key_id),
        organization_id=UUID(org.id),
        actor_user_id=UUID(user),
        actor_org_role=OrganizationRole.MEMBER,
        request=None,
    )


async def feed(org: SimpleNamespace, user: str, organization: str | None = None, **params):
    async with client_for(org, user, organization) as client:
        return await client.get("/api/net/feed", params=params)


async def settle(org: SimpleNamespace) -> None:
    """Age every message past the settle window instead of sleeping through it."""
    await org.content.execute_query(
        f"UPDATE net_messages SET created_at = created_at - {SETTLE_SECONDS + 5}s "  # noqa: S608
        "WHERE organization_id = $org;",
        org=org.id,
    )


async def stored_messages(org: SimpleNamespace) -> list[dict[str, object]]:
    return content_client.normalize_records(
        await org.content.execute_query(
            "SELECT * FROM net_messages WHERE organization_id = $org;", org=org.id
        )
    )


async def stored_captures(org: SimpleNamespace) -> list[dict[str, object]]:
    return content_client.normalize_records(
        await org.content.execute_query(
            "SELECT * FROM raw_captures WHERE organization_id = $org;", org=org.id
        )
    )


async def test_members_post_and_every_reader_sees_it(org: SimpleNamespace) -> None:
    posted = await post(
        org,
        org.ada,
        channel=" #Reviews ",
        body="  Use keyset paging for the feed.  ",
        thread="gh:acme/app#42",
        agent="claude-code",
        session="session-1",
    )

    assert posted.status_code == 201, posted.text
    message = posted.json()["message"]
    assert posted.json()["decision_memory_id"] is None
    assert message["channel"] == "#reviews"
    assert message["thread"] == "gh:acme/app#42"
    assert message["kind"] == "message"
    assert message["body"] == "Use keyset paging for the feed."
    assert message["author"] == {
        "user_id": org.ada,
        "name": "Ada",
        "email": email(org, "Ada"),
        "agent": "claude-code",
        "session": "session-1",
    }
    assert message["api_key_id"] is None
    assert message["project_id"] is None
    assert NetCursor.decode(message["cursor"]).uuid == message["id"]

    await settle(org)
    for reader in (org.grace, org.vic):
        read = await feed(org, reader)
        assert read.status_code == 200, read.text
        assert [m["id"] for m in read.json()["messages"]] == [message["id"]]
        assert read.json()["more"] is False
        assert read.json()["next_since"] == read.json()["messages"][0]["cursor"]

    elsewhere = await feed(org, org.ozzy, organization=org.other_org)
    assert elsewhere.status_code == 200
    assert elsewhere.json()["messages"] == []


async def test_viewers_cannot_post(org: SimpleNamespace) -> None:
    refused = await post(org, org.vic, channel="#reviews", body="hello")

    assert refused.status_code == 403
    assert await stored_messages(org) == []


@pytest.mark.parametrize(
    "body",
    [
        {"channel": "bad channel", "body": "x"},
        {"channel": "#ok", "body": "   "},
        {"channel": "#ok", "body": "x" * 16_001},
        {"channel": "#ok", "body": "x", "thread": "two\nlines"},
        {"channel": "#ok", "body": "x", "thread": "t" * 201},
        {"channel": "#ok", "body": "x", "agent": "a" * 65},
        {"channel": "#ok", "body": "x", "session": "s" * 129},
        {"channel": "#ok", "body": "x", "kind": "chat"},
        {"channel": "#ok", "body": "x", "agent": "x)\n[2026-11-02T10:00:00Z] Grace"},
        {"channel": "#ok", "body": "x", "session": "s\u2028t"},
    ],
)
async def test_malformed_posts_are_refused(org: SimpleNamespace, body: dict[str, object]) -> None:
    refused = await post(org, org.ada, **body)

    assert refused.status_code == 422, refused.text
    assert await stored_messages(org) == []


@pytest.mark.parametrize(
    "params",
    [
        {"since": "garbage"},
        # Cursors in the older ISO-instant format, off the calendar or on it.
        {"since": base64.urlsafe_b64encode(b"2026-13-45T00:00:00Z|").decode().rstrip("=")},
        {"since": base64.urlsafe_b64encode(b"2026-11-02T10:00:00.5Z|").decode().rstrip("=")},
        {"since": NetCursor(created_ns=2**63, uuid="").encode()},
        {"channel": [f"c{index}" for index in range(33)]},
        {"channel": "not a channel"},
        {"thread": "bell\x07"},
        {"limit": 501},
        {"wait": 26},
    ],
)
async def test_malformed_feed_reads_are_refused(
    org: SimpleNamespace, params: dict[str, object]
) -> None:
    refused = await feed(org, org.ada, **params)

    assert refused.status_code == 422, refused.text


async def test_naming_a_project_takes_contributor_access(org: SimpleNamespace) -> None:
    hidden = await post(org, org.ada, channel="#reviews", body="x", project_id=org.secret)
    missing = await post(org, org.ada, channel="#reviews", body="x", project_id="project_nope")
    read_only = await post(org, org.ada, channel="#reviews", body="x", project_id=org.readonly)
    writable = await post(org, org.ada, channel="#reviews", body="x", project_id=org.shared)

    assert hidden.status_code == 403, hidden.text
    assert missing.status_code == 404, missing.text
    assert read_only.status_code == 403, read_only.text
    assert writable.status_code == 201, writable.text
    assert writable.json()["message"]["project_id"] == org.shared
    assert len(await stored_messages(org)) == 1


async def test_a_restricted_key_cannot_tag_a_project_outside_its_grants(
    org: SimpleNamespace,
) -> None:
    _, secret = await api_key(org, org.ada, projects=[org.shared])

    outside = await post(
        org, org.ada, token=secret, channel="#reviews", body="x", project_id=org.other
    )
    inside = await post(
        org, org.ada, token=secret, channel="#reviews", body="x", project_id=org.shared
    )
    unscoped = await post(org, org.ada, token=secret, channel="#reviews", body="no project")

    assert outside.status_code == 403, outside.text
    assert outside.json()["detail"] == "api_key_project_denied"
    assert inside.status_code == 201, inside.text
    assert inside.json()["message"]["api_key_id"]
    assert unscoped.status_code == 201, unscoped.text
    assert [row["body"] for row in await stored_messages(org)].count("x") == 1


async def test_a_pinned_thread_refuses_only_a_plain_retarget(org: SimpleNamespace) -> None:
    pinned = await post(
        org, org.grace, channel="#reviews", thread="t", body="start", project_id=org.other
    )
    retarget = await post(
        org, org.ada, channel="#reviews", thread="t", body="move", project_id=org.shared
    )
    viewer = await post(
        org, org.ada, channel="#reviews", thread="t", body="move", project_id=org.readonly
    )
    plain = await post(org, org.ada, channel="#reviews", thread="t", body="no project")
    same = await post(
        org, org.ada, channel="#reviews", thread="t", body="same", project_id=org.other
    )
    decided = await post(
        org,
        org.ada,
        channel="#reviews",
        thread="t",
        body="Decided in our own project",
        kind="decision",
        project_id=org.shared,
    )

    assert pinned.status_code == 201, pinned.text
    assert retarget.status_code == 409, retarget.text
    assert retarget.json()["detail"]["error"] == "thread_project_pinned"
    assert retarget.json()["detail"]["project_id"] == org.other
    assert viewer.status_code == 403, viewer.text
    assert plain.status_code == 201, plain.text
    assert same.status_code == 201, same.text
    # A decision is accepted into a thread pinned elsewhere: its memory goes
    # to its own project and the pin stays where it was.
    assert decided.status_code == 201, decided.text
    assert decided.json()["message"]["project_id"] == org.shared
    [memory] = await stored_captures(org)
    assert memory["uuid"] == decided.json()["decision_memory_id"]
    assert (memory["memory_scope"], memory["scope_key"]) == ("project", org.shared)
    async with client_for(org, org.vic) as client:
        threads = (await client.get("/api/net/threads", params={"channel": "#reviews"})).json()
    assert [(t["thread"], t["project_id"], t["message_count"]) for t in threads["threads"]] == [
        ("t", org.other, 4)
    ]


async def test_agents_from_different_projects_share_one_inbox(org: SimpleNamespace) -> None:
    # Ada works in a repository linked to the shared project, Grace in one
    # linked to the other; the CLI names a project only on their decisions.
    posts = [
        await post(org, org.ada, channel="@vic", body="Review my branch?"),
        await post(org, org.grace, channel="@vic", body="Mine too"),
        await post(
            org,
            org.ada,
            channel="@vic",
            body="Keyset paging",
            kind="decision",
            project_id=org.shared,
        ),
        await post(
            org,
            org.grace,
            channel="@vic",
            body="Two-phase migration",
            kind="decision",
            project_id=org.other,
        ),
        # Naming a project explicitly on a threadless post pins nothing either.
        await post(org, org.ada, channel="@vic", body="FYI", project_id=org.shared),
        await post(org, org.grace, channel="@vic", body="FYI too", project_id=org.other),
    ]

    assert [response.status_code for response in posts] == [201] * len(posts)
    memories = {row["title"]: row for row in await stored_captures(org)}
    assert memories["Keyset paging"]["metadata"]["project_id"] == org.shared
    assert memories["Two-phase migration"]["metadata"]["project_id"] == org.other
    async with client_for(org, org.vic) as client:
        threads = (await client.get("/api/net/threads", params={"channel": "@vic"})).json()
    assert [(t["thread"], t["project_id"], t["message_count"]) for t in threads["threads"]] == [
        (None, None, 6)
    ]


async def test_a_project_viewer_can_post_plain_messages(org: SimpleNamespace) -> None:
    # Ada only views the read-only project; a plain post from its repository
    # names no project, so nothing about that project is checked.
    plain = await post(org, org.ada, channel="#readonly-room", body="Question about the API")
    threaded = await post(
        org, org.ada, channel="#readonly-room", thread="q1", body="Follow-up question"
    )

    assert plain.status_code == 201, plain.text
    assert threaded.status_code == 201, threaded.text


async def test_a_retried_decision_lands_once(org: SimpleNamespace) -> None:
    body = {
        "channel": "#decisions",
        "body": "Adopt keyset paging",
        "kind": "decision",
        "project_id": org.shared,
    }
    headers = {"Idempotency-Key": f"net-post-{uuid4()}"}

    first = await post(org, org.ada, headers=headers, **body)
    again = await post(org, org.ada, headers=headers, **body)
    different = await post(org, org.ada, headers=headers, **{**body, "body": "Something else"})

    assert first.status_code == 201, first.text
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    assert different.status_code == 409, different.text
    assert len(await stored_messages(org)) == 1
    assert [row["uuid"] for row in await stored_captures(org)] == [
        first.json()["decision_memory_id"]
    ]


async def test_deleting_the_organization_purges_its_channels(
    org: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = organization_runtime
    assert (await post(org, org.ada, channel="#reviews", thread="t", body="x")).status_code == 201
    survivor = await post_elsewhere(org)
    monkeypatch.setattr(runtime, "delete_graph_data", AsyncMock())
    monkeypatch.setattr(runtime, "announce_sessions_invalidated", AsyncMock())

    await runtime.delete_org(request=None, slug=org.slug, user_id=UUID(org.owen))  # type: ignore[arg-type]

    remaining = content_client.normalize_records(
        await org.content.execute_query(
            "SELECT organization_id FROM net_messages WHERE organization_id IN $orgs; "
            "SELECT organization_id FROM net_threads WHERE organization_id IN $orgs;",
            orgs=[org.id, org.other_org],
        )
    )
    assert {row["organization_id"] for row in remaining} == {org.other_org}
    assert survivor.channel == "#elsewhere"


async def post_elsewhere(org: SimpleNamespace):
    from sibyl_core.services.net import post_net_message

    return await post_net_message(
        organization_id=org.other_org,
        channel="#elsewhere",
        thread="",
        kind="message",
        body="stays",
        author_user_id=org.ozzy,
    )


async def test_a_decision_is_remembered_before_it_is_posted(org: SimpleNamespace) -> None:
    posted = await post(
        org,
        org.ada,
        channel="#decisions",
        thread="gh:acme/app#42",
        kind="decision",
        project_id=org.shared,
        agent="claude-code",
        body="\n  Adopt keyset paging everywhere\nOffset paging rescans the index.",
    )

    assert posted.status_code == 201, posted.text
    message = posted.json()["message"]
    memory_id = posted.json()["decision_memory_id"]
    assert memory_id
    assert message["decision_memory_id"] == memory_id
    assert message["kind"] == "decision"
    rows = await stored_captures(org)
    assert [row["uuid"] for row in rows] == [memory_id]
    memory = rows[0]
    assert memory["title"] == "Adopt keyset paging everywhere"
    assert memory["raw_content"] == (
        "Adopt keyset paging everywhere\nOffset paging rescans the index.\n\n"
        "Recorded in sibylnet channel #decisions, thread gh:acme/app#42, by Ada (claude-code)."
    )
    assert memory["principal_id"] == org.ada
    # The shared project's default write scope is the project, as remember resolves it.
    assert (memory["memory_scope"], memory["scope_key"]) == ("project", org.shared)
    assert "sibylnet" in memory["tags"]
    assert memory["metadata"]["sibylnet"] == {
        "channel": "#decisions",
        "thread": "gh:acme/app#42",
        "message_id": message["id"],
    }
    assert memory["metadata"]["remember_kind"] == "decision"


async def test_a_refused_decision_posts_nothing(org: SimpleNamespace) -> None:
    refused = await post(
        org,
        org.ada,
        channel="#decisions",
        kind="decision",
        project_id=org.readonly,
        body="Ship it",
    )

    assert refused.status_code == 403, refused.text
    assert await stored_messages(org) == []
    assert await stored_captures(org) == []


async def test_a_watcher_starts_from_now_and_long_polls(org: SimpleNamespace) -> None:
    assert (await post(org, org.ada, channel="#reviews", body="history")).status_code == 201
    await settle(org)
    start = await feed(org, org.grace, limit=0)
    assert start.status_code == 200
    assert start.json()["messages"] == []
    since = start.json()["next_since"]

    async def post_later() -> None:
        await asyncio.sleep(0.5)
        assert (await post(org, org.ada, channel="#reviews", body="news")).status_code == 201

    waited, _ = await asyncio.gather(feed(org, org.grace, since=since, wait=10), post_later())

    assert waited.status_code == 200, waited.text
    assert [m["body"] for m in waited.json()["messages"]] == ["news"]
    resumed = await feed(org, org.grace, since=waited.json()["next_since"])
    assert resumed.json()["messages"] == []
    assert resumed.json()["next_since"] == waited.json()["next_since"]


@pytest.fixture
def feed_reads(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """When each feed query ran, as the route issued them."""
    reads: list[float] = []
    real = net_routes.read_net_feed

    async def counted(**kwargs: object):
        reads.append(asyncio.get_running_loop().time())
        return await real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(net_routes, "read_net_feed", counted)
    return reads


async def test_a_post_wakes_a_long_poll_without_polling(
    org: SimpleNamespace, feed_reads: list[float]
) -> None:
    start = await feed(org, org.grace, channel="#reviews", limit=0)
    since = start.json()["next_since"]
    feed_reads.clear()

    async def post_later() -> float:
        await asyncio.sleep(0.5)
        assert (await post(org, org.ada, channel="#reviews", body="news")).status_code == 201
        return asyncio.get_running_loop().time()

    begun = asyncio.get_running_loop().time()
    waited, posted_at = await asyncio.gather(
        feed(org, org.grace, channel="#reviews", since=since, wait=20), post_later()
    )
    finished = asyncio.get_running_loop().time()

    assert [m["body"] for m in waited.json()["messages"]] == ["news"]
    # One read before the wait, one once the message settled: no
    # once-a-second polling in between.
    assert len(feed_reads) == 2
    assert SETTLE_SECONDS <= finished - posted_at < SETTLE_SECONDS + 2
    assert finished - begun < SETTLE_SECONDS + 3


async def test_a_post_elsewhere_does_not_wake_a_long_poll(
    org: SimpleNamespace, feed_reads: list[float]
) -> None:
    start = await feed(org, org.grace, channel="#quiet", limit=0)
    since = start.json()["next_since"]
    feed_reads.clear()

    async def post_elsewhere() -> None:
        await asyncio.sleep(0.2)
        assert (await post(org, org.ada, channel="#loud", body="x")).status_code == 201

    waited, _ = await asyncio.gather(
        feed(org, org.grace, channel="#quiet", since=since, wait=7), post_elsewhere()
    )

    assert waited.json()["messages"] == []
    # The first read, one safety recheck at five seconds, and the last read
    # when the wait runs out: no reads driven by the other channel's post.
    assert len(feed_reads) == 3


async def test_channels_and_threads_list_newest_first(org: SimpleNamespace) -> None:
    for body in (
        {"channel": "#builds", "body": "nightly green", "thread": "nightly"},
        {"channel": "#reviews", "body": "first", "thread": "gh:acme/app#1"},
        {"channel": "#reviews", "body": "second", "thread": "gh:acme/app#1"},
        {"channel": "#reviews", "body": "loose", "project_id": org.shared},
    ):
        assert (await post(org, org.ada, **body)).status_code == 201

    async with client_for(org, org.vic) as client:
        channels = await client.get("/api/net/channels")
        threads = await client.get("/api/net/threads", params={"channel": "#Reviews"})
        bad = await client.get("/api/net/threads", params={"channel": "no spaces"})

    assert channels.status_code == 200, channels.text
    assert [
        (c["channel"], c["message_count"], c["thread_count"]) for c in channels.json()["channels"]
    ] == [("#reviews", 3, 1), ("#builds", 1, 1)]
    assert threads.status_code == 200, threads.text
    listed = threads.json()["threads"]
    assert [(t["thread"], t["message_count"], t["project_id"]) for t in listed] == [
        # The threadless row never takes a project, even from a post that names one.
        (None, 1, None),
        ("gh:acme/app#1", 2, None),
    ]
    assert listed[1]["reflected_through"] is None
    assert listed[1]["first_message_at"] <= listed[1]["last_message_at"]
    assert bad.status_code == 422
