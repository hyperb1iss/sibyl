"""Team activity reads only what the caller could already read.

Auth, content and graph run on the embedded engine with persisted members,
projects and grants; identity comes from signed tokens resolved by the native
resolver. Each test reads the public route over HTTP as one member and checks
both the per-person counts and the recent feed.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import SecretStr

from sibyl.api.routes import activity
from sibyl.auth.jwt import create_access_token, verify_access_token
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl.persistence.surreal import content as surreal_content, organization_runtime
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver
from sibyl.persistence.surreal.auth_runtime import _common as auth_common
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
)
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.config import settings
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import content_client, graph_client
from sibyl_core.services.graph import SurrealGraphClient
from sibyl_core.services.graph_client import prepare_graph_schema
from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

NOW = datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def engine_url(engine: str) -> str:
    """The embedded engine, or a live server when SIBYL_LIVE_SURREAL_TESTS=1 names one.

    The embedded engine is more lenient than a 3.x server, so every statement
    here also runs against a server when one is configured.
    """
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


CREDENTIALS = {
    "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
    "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
}


@pytest.fixture(params=["embedded", "live"])
async def team(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SimpleNamespace]:
    url = engine_url(request.param)
    namespace = f"verify_team_activity_{uuid4().hex}"
    auth = SurrealAuthClient(url=url, namespace=namespace, **CREDENTIALS)
    content = SurrealContentClient(url=url, namespace=namespace, **CREDENTIALS)
    monkeypatch.setattr(settings, "surreal_url", url)
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("isolated-team-activity-proof-" * 3))
    await bootstrap_auth_schema(auth)
    await bootstrap_content_schema(content)

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
        request.state.auth_context = await resolver.resolve(verify_access_token(token))
        return await call_next(request)

    app.include_router(activity.router, prefix="/api")
    runtime = SimpleNamespace(
        auth=auth,
        content=content,
        app=app,
        org=str(uuid4()),
        alice=str(uuid4()),
        bob=str(uuid4()),
        carol=str(uuid4()),
        outsider=str(uuid4()),
        shared=_id("project"),
        secret=_id("project"),
    )
    await seed_auth(runtime)
    try:
        yield runtime
    finally:
        for group in graphs:
            graph_client._clients.pop(group, None)
            graph_client.mark_graph_schema_dirty(group)
        if request.param == "live":
            for client in (*graphs.values(), auth):
                await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        for client in graphs.values():
            await client.close()
        await content.close()
        await auth.close()


async def seed_auth(team: SimpleNamespace) -> None:
    names = {team.alice: "Alice", team.bob: "Bob", team.carol: "Carol", team.outsider: "Ozzy"}
    project_ids = {team.shared: str(uuid4()), team.secret: str(uuid4())}
    records = {
        "users": [
            {"uuid": user, "email": f"{name.lower()}@example.test", "name": name}
            for user, name in names.items()
        ],
        "organizations": [{"uuid": team.org, "name": "Coven", "slug": f"coven-{team.org[:8]}"}],
        "organization_members": [
            {"uuid": str(uuid4()), "organization_id": team.org, "user_id": user, "role": role}
            for user, role in ((team.alice, "member"), (team.bob, "member"), (team.carol, "admin"))
        ],
        "projects": [
            {
                "uuid": record_id,
                "organization_id": team.org,
                "name": graph_id,
                "slug": graph_id,
                "graph_project_id": graph_id,
                "visibility": "private",
            }
            for graph_id, record_id in project_ids.items()
        ],
        "project_members": [
            {
                "uuid": str(uuid4()),
                "organization_id": team.org,
                "project_id": project_ids[graph_id],
                "user_id": user,
                "role": "project_contributor",
            }
            for graph_id, user in (
                (team.shared, team.alice),
                (team.shared, team.bob),
                (team.secret, team.alice),
            )
        ],
    }
    for table, rows in records.items():
        for record in rows:
            await team.auth.execute_query(f"CREATE {table} CONTENT $record;", record=record)


def token_for(team: SimpleNamespace, user: str, org: str | None = None) -> str:
    return create_access_token(user_id=UUID(user), organization_id=UUID(org or team.org))


async def read_activity(
    team: SimpleNamespace, user: str, *, org: str | None = None, **params: str | list[str]
) -> httpx.Response:
    transport = httpx.ASGITransport(app=team.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://activity.test") as client:
        return await client.get(
            "/api/activity/team",
            params=params,
            headers={"Authorization": f"Bearer {token_for(team, user, org)}"},
        )


def entity(
    entity_type: EntityType,
    name: str,
    *,
    author: str | None,
    age: timedelta = timedelta(hours=1),
    project: str | None = None,
    scope: str | None = None,
    metadata: dict[str, object] | None = None,
    modified_by: str | None = None,
    touched: timedelta | None = None,
    entity_id: str | None = None,
) -> Entity:
    bag: dict[str, object] = dict(metadata or {})
    if project:
        bag["project_id"] = project
    if scope == "private":
        bag.update(memory_scope="private", principal_id=author)
    elif scope == "project":
        bag.update(memory_scope="project", scope_key=project, principal_id=author)
    created = NOW - age
    return Entity(
        id=entity_id or _id(entity_type.value),
        entity_type=entity_type,
        name=name,
        description=name,
        created_by=author,
        modified_by=modified_by,
        created_at=created,
        updated_at=created if touched is None else NOW - touched,
        metadata=bag,
    )


def capture(
    title: str,
    *,
    principal: str,
    org: str,
    scope: str,
    project: str | None = None,
    age: timedelta = timedelta(hours=1),
    **fields: object,
) -> RawCaptureRecord:
    created = (NOW - age).replace(tzinfo=None)
    return RawCaptureRecord(
        organization_id=UUID(org),
        title=title,
        raw_content=f"{title} body",
        entity_type=str(fields.pop("entity_type", "raw_memory")),
        principal_id=principal,
        memory_scope=scope,
        scope_key=project if scope == "project" else None,
        project_id=project,
        created_by_user_id=UUID(principal),
        captured_at=created,
        created_at=created,
        **fields,  # type: ignore[arg-type]
    )


@pytest.fixture
async def seeded(team: SimpleNamespace) -> SimpleNamespace:
    """One organization's week, with a row behind every visibility rule."""
    rows = {
        "shared_decision": entity(
            EntityType.DECISION,
            "Adopt keyset paging",
            author=team.alice,
            project=team.shared,
            scope="project",
        ),
        "private_decision": entity(
            EntityType.DECISION, "Alice's private hunch", author=team.alice, scope="private"
        ),
        "secret_task": entity(
            EntityType.TASK,
            "Secret project task",
            author=team.alice,
            project=team.secret,
            metadata={"status": "todo"},
        ),
        "bob_task": entity(
            EntityType.TASK,
            "Bob ships the migration",
            author=team.bob,
            project=team.shared,
            age=timedelta(hours=5),
            metadata={
                "status": "done",
                "completed_at": (NOW - timedelta(hours=2)).isoformat(),
                "completed_by": team.bob,
            },
        ),
        "patched_done": entity(
            EntityType.TASK,
            "Closed by a patch",
            author=team.alice,
            project=team.shared,
            age=timedelta(days=20),
            touched=timedelta(hours=1),
            modified_by=team.bob,
            metadata={"status": "done"},
        ),
        "legacy_done": entity(
            EntityType.TASK,
            "Completed before the stamp",
            author=team.alice,
            project=team.shared,
            age=timedelta(days=20),
            touched=timedelta(hours=3),
            modified_by=team.bob,
            metadata={"status": "done", "completed_at": (NOW - timedelta(hours=3)).isoformat()},
        ),
        "archived_note": entity(
            EntityType.NOTE,
            "Archived note",
            author=team.alice,
            project=team.shared,
            scope="project",
            metadata={"archived": True},
        ),
        "sensitive_decision": entity(
            EntityType.DECISION,
            "Credential rotation detail",
            author=team.alice,
            project=team.shared,
            scope="project",
            metadata={"lifecycle_flags": ["sensitive"]},
        ),
        "hidden_procedure": entity(
            EntityType.PROCEDURE,
            "Hidden procedure",
            author=team.bob,
            project=team.shared,
            scope="project",
            metadata={"review_state": "hidden"},
        ),
        "passage": entity(
            EntityType.PASSAGE,
            "Derived span",
            author=team.alice,
            project=team.shared,
            scope="project",
            metadata={"category": "passage_projection"},
        ),
        "reflection": entity(
            EntityType.DECISION,
            "Dream cycle synthesis",
            author=team.alice,
            project=team.shared,
            scope="project",
            metadata={"capture_mode": "reflect", "capture_surface": "reflection"},
        ),
        "old_decision": entity(
            EntityType.DECISION,
            "Ten days back",
            author=team.bob,
            project=team.shared,
            scope="project",
            age=timedelta(days=10),
        ),
        "outsider_note": entity(
            EntityType.NOTE,
            "Written by someone who left",
            author=team.outsider,
            project=team.shared,
            scope="project",
        ),
        "async_memory": entity(
            EntityType.PROCEDURE,
            "Async procedure without created_by",
            author=None,
            project=team.shared,
            metadata={
                "memory_scope": "project",
                "scope_key": team.shared,
                "principal_id": team.bob,
            },
        ),
        "artifact": entity(
            EntityType.ARTIFACT,
            "Runbook artifact",
            author=team.bob,
            project=team.shared,
            scope="project",
        ),
    }
    # The projects themselves, created long before the window and by nobody,
    # so they only lend their names to the feed.
    projects = [
        entity(
            EntityType.PROJECT,
            name,
            author=None,
            age=timedelta(days=90),
            entity_id=project_id,
        )
        for project_id, name in ((team.shared, "Shared Board"), (team.secret, "Secret Lab"))
    ]
    graph = await get_surreal_graph_runtime(team.org)
    await graph.entity_manager.create_direct_bulk([*projects, *rows.values()])

    captures = {
        "alice_private": capture(
            "Alice private capture", principal=team.alice, org=team.org, scope="private"
        ),
        "bob_shared": capture(
            "Bob shared capture",
            principal=team.bob,
            org=team.org,
            scope="project",
            project=team.shared,
        ),
        "alice_secret": capture(
            "Alice secret capture",
            principal=team.alice,
            org=team.org,
            scope="project",
            project=team.secret,
        ),
        "bob_hidden": capture(
            "Bob hidden capture",
            principal=team.bob,
            org=team.org,
            scope="project",
            project=team.shared,
            metadata={"lifecycle_flags": ["hidden"]},
        ),
        "bob_sensitive": capture(
            "Bob token paste",
            principal=team.bob,
            org=team.org,
            scope="project",
            project=team.shared,
            metadata={"sensitive": True},
        ),
        "bob_deleted": capture(
            "Bob deleted capture",
            principal=team.bob,
            org=team.org,
            scope="project",
            project=team.shared,
            deleted_at=NOW.replace(tzinfo=None),
        ),
        "bob_sidecar": capture(
            "Bob sidecar",
            principal=team.bob,
            org=team.org,
            scope="project",
            project=team.shared,
            entity_type="decision",
            entity_id=rows["shared_decision"].id,
        ),
        "bob_projected": capture(
            "Bob projected raw",
            principal=team.bob,
            org=team.org,
            scope="project",
            project=team.shared,
            metadata={"projected_capture_id": str(uuid4())},
        ),
        "bob_old": capture(
            "Bob old capture",
            principal=team.bob,
            org=team.org,
            scope="project",
            project=team.shared,
            age=timedelta(days=40),
        ),
    }
    for record in captures.values():
        await surreal_content.save_raw_capture_record(None, capture=record)
    team.rows = rows
    team.captures = captures
    return team


def person(payload: dict, user: str) -> dict:
    return next(row for row in payload["people"] if row["user_id"] == user)


def listed(payload: dict) -> set[tuple[str, str]]:
    return {(item["kind"], item["id"]) for item in payload["recent"]}


async def test_teammate_sees_only_rows_they_could_already_read(seeded) -> None:
    team = seeded
    response = await read_activity(team, team.bob)
    assert response.status_code == 200, response.text
    payload = response.json()
    rows, captures = team.rows, team.captures

    assert person(payload, team.alice)["counts"] == {
        "captures": 0,
        "tasks_created": 0,
        "tasks_completed": 0,
        "decisions": 1,
        "notes": 0,
        "procedures": 0,
        "other": 0,
    }
    assert person(payload, team.bob)["counts"] == {
        "captures": 1,
        "tasks_created": 1,
        "tasks_completed": 1,
        "decisions": 0,
        "notes": 0,
        "procedures": 1,
        "other": 1,
    }
    assert listed(payload) == {
        ("decision", rows["shared_decision"].id),
        ("task_created", rows["bob_task"].id),
        ("task_completed", rows["bob_task"].id),
        ("procedure", rows["async_memory"].id),
        ("entity", rows["artifact"].id),
        ("capture", str(captures["bob_shared"].id)),
    }
    # Nothing private, unreadable or retired reaches the page in any field.
    text = json.dumps(payload)
    for hidden in (
        rows["private_decision"],
        rows["secret_task"],
        rows["archived_note"],
        rows["sensitive_decision"],
        rows["hidden_procedure"],
    ):
        assert hidden.id not in text
        assert hidden.name not in text
    for hidden_capture in (
        "alice_private",
        "alice_secret",
        "bob_hidden",
        "bob_sensitive",
        "bob_deleted",
    ):
        assert str(captures[hidden_capture].id) not in text
        assert captures[hidden_capture].title not in text
    assert payload["truncated"] is False


async def test_owner_sees_their_private_activity(seeded) -> None:
    team = seeded
    response = await read_activity(team, team.alice)
    assert response.status_code == 200, response.text
    payload = response.json()
    counts = person(payload, team.alice)["counts"]
    # Shared and private decisions, the secret task, both own captures.
    assert counts["decisions"] == 2
    assert counts["tasks_created"] == 1
    assert counts["captures"] == 2
    ids = listed(payload)
    assert ("decision", team.rows["private_decision"].id) in ids
    assert ("task_created", team.rows["secret_task"].id) in ids
    assert ("capture", str(team.captures["alice_private"].id)) in ids
    assert ("capture", str(team.captures["alice_secret"].id)) in ids
    # Bob's captures are counted the same for Alice as for Bob.
    assert person(payload, team.bob)["counts"]["captures"] == 1


async def test_every_member_is_listed_and_ordered_by_activity(seeded) -> None:
    team = seeded
    payload = (await read_activity(team, team.bob)).json()
    assert [row["user_id"] for row in payload["people"]] == [team.bob, team.alice, team.carol]
    carol = person(payload, team.carol)
    assert carol["role"] == "admin"
    assert carol["name"] == "Carol"
    assert carol["email"] == "carol@example.test"
    assert set(carol["counts"].values()) == {0}
    assert carol["last_active_at"] is None
    # The departed author is no member, so nobody is credited for the row.
    assert team.outsider not in {row["user_id"] for row in payload["people"]}
    assert team.rows["outsider_note"].id not in json.dumps(payload)
    bob = person(payload, team.bob)
    newest = max(item["at"] for item in payload["recent"] if item["actor_id"] == team.bob)
    assert datetime.fromisoformat(bob["last_active_at"]) == datetime.fromisoformat(newest)
    stamps = [datetime.fromisoformat(item["at"]) for item in payload["recent"]]
    assert stamps == sorted(stamps, reverse=True)


async def test_hrefs_point_at_existing_pages(seeded) -> None:
    team = seeded
    payload = (await read_activity(team, team.bob)).json()
    hrefs = {(item["kind"], item["id"]): item["href"] for item in payload["recent"]}
    bob_task = team.rows["bob_task"].id
    assert hrefs[("task_created", bob_task)] == f"/tasks/{bob_task}"
    decision = team.rows["shared_decision"].id
    assert hrefs[("decision", decision)] == f"/entities/{decision}"
    capture_id = str(team.captures["bob_shared"].id)
    assert hrefs[("capture", capture_id)] == f"/memory/captures?id={capture_id}"


async def test_window_bounds_the_counts(seeded) -> None:
    team = seeded
    day = (await read_activity(team, team.bob, window="24h")).json()
    month = (await read_activity(team, team.bob, window="30d")).json()
    assert day["window"]["label"] == "24h"
    since = datetime.fromisoformat(day["window"]["since"])
    until = datetime.fromisoformat(day["window"]["until"])
    assert until - since == timedelta(hours=24)
    # Ten days back counts in the month only; the 40-day capture never does.
    assert person(day, team.bob)["counts"]["decisions"] == 0
    assert person(month, team.bob)["counts"]["decisions"] == 1
    assert person(month, team.bob)["counts"]["captures"] == 1
    assert ("decision", team.rows["old_decision"].id) in listed(month)
    assert str(team.captures["bob_old"].id) not in json.dumps(month)
    # A done task with no completion record (here, an old patch) credits
    # nobody: its last editor is not necessarily who finished it.
    assert ("task_completed", team.rows["patched_done"].id) not in listed(month)
    # Both tasks Alice created 20 days ago count in the month and not the week.
    assert person(month, team.alice)["counts"]["tasks_created"] == 2
    assert person(day, team.alice)["counts"]["tasks_created"] == 0
    # The pre-stamp completion names no actor, so it credits nobody anywhere.
    assert ("task_completed", team.rows["legacy_done"].id) not in listed(month)


async def test_unknown_window_is_rejected(seeded) -> None:
    response = await read_activity(seeded, seeded.bob, window="1y")
    assert response.status_code == 422
    blank = await read_activity(seeded, seeded.bob, project_id=[seeded.shared, " "])
    assert blank.status_code == 422


async def test_project_filter_keeps_one_readable_project(seeded) -> None:
    team = seeded
    response = await read_activity(team, team.alice, project_id=team.shared)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["project_id"] == team.shared
    assert payload["project_ids"] == [team.shared]
    assert {item["project_id"] for item in payload["recent"]} == {team.shared}
    text = json.dumps(payload)
    assert team.rows["secret_task"].id not in text
    assert team.rows["private_decision"].id not in text
    assert str(team.captures["alice_private"].id) not in text
    assert person(payload, team.alice)["counts"]["decisions"] == 1


async def test_project_filter_cannot_open_a_project_the_caller_lacks(seeded) -> None:
    team = seeded
    response = await read_activity(team, team.bob, project_id=team.secret)
    assert response.status_code == 403
    assert team.rows["secret_task"].id not in response.text
    assert str(team.captures["alice_secret"].id) not in response.text
    missing = await read_activity(team, team.bob, project_id=_id("project"))
    assert missing.status_code == 403
    # A real project and an invented one are refused alike, so the answer says
    # nothing about which projects exist.
    assert missing.json() == response.json()


async def test_several_projects_count_activity_in_any_of_them(seeded) -> None:
    team = seeded
    response = await read_activity(team, team.alice, project_id=[team.shared, team.secret])
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["project_id"] is None
    assert payload["project_ids"] == [team.shared, team.secret]
    ids = listed(payload)
    assert ("task_created", team.rows["secret_task"].id) in ids
    assert ("decision", team.rows["shared_decision"].id) in ids
    assert ("capture", str(team.captures["alice_secret"].id)) in ids
    assert {item["project_id"] for item in payload["recent"]} == {team.shared, team.secret}
    text = json.dumps(payload)
    assert team.rows["private_decision"].id not in text
    assert str(team.captures["alice_private"].id) not in text


async def test_several_projects_are_refused_when_any_is_unreadable(seeded) -> None:
    team = seeded
    response = await read_activity(team, team.bob, project_id=[team.shared, team.secret])
    assert response.status_code == 403
    assert team.rows["secret_task"].id not in response.text
    assert "Secret Lab" not in response.text


async def test_feed_names_actors_and_only_readable_projects(seeded) -> None:
    team = seeded
    bob_view = (await read_activity(team, team.bob)).json()
    items = {(item["kind"], item["id"]): item for item in bob_view["recent"]}
    decision = items[("decision", team.rows["shared_decision"].id)]
    assert (decision["actor_name"], decision["project_name"]) == ("Alice", "Shared Board")
    assert decision["actor_avatar_url"] is None
    assert items[("capture", str(team.captures["bob_shared"].id))]["actor_name"] == "Bob"
    assert "Secret Lab" not in json.dumps(bob_view)

    alice_view = (await read_activity(team, team.alice)).json()
    items = {(item["kind"], item["id"]): item for item in alice_view["recent"]}
    assert items[("task_created", team.rows["secret_task"].id)]["project_name"] == "Secret Lab"
    # A private memory belongs to no project, so it names none.
    assert items[("decision", team.rows["private_decision"].id)]["project_name"] is None


async def test_actor_filter_narrows_recent_but_not_people(seeded) -> None:
    team = seeded
    everyone = (await read_activity(team, team.bob)).json()
    alice_only = (await read_activity(team, team.bob, actor_id=team.alice)).json()
    assert alice_only["actor_id"] == team.alice
    assert alice_only["people"] == everyone["people"]
    assert {item["actor_id"] for item in alice_only["recent"]} == {team.alice}
    assert listed(alice_only) == {("decision", team.rows["shared_decision"].id)}
    stranger = (await read_activity(team, team.bob, actor_id=str(uuid4()))).json()
    assert stranger["recent"] == []
    assert stranger["people"] == everyone["people"]


async def test_a_remember_counts_once_whichever_path_wrote_it(seeded) -> None:
    """MCP writes the raw memory and the graph row with no sidecar: still one act."""
    team = seeded
    raw = capture(
        "Bob remembers over MCP",
        principal=team.bob,
        org=team.org,
        scope="project",
        project=team.shared,
    )
    await surreal_content.save_raw_capture_record(None, capture=raw)
    decision = entity(
        EntityType.DECISION,
        "Bob remembers over MCP",
        author=team.bob,
        project=team.shared,
        scope="project",
        metadata={"raw_memory_id": str(raw.id)},
    )
    graph = await get_surreal_graph_runtime(team.org)
    await graph.entity_manager.create_direct_bulk([decision])

    payload = (await read_activity(team, team.alice)).json()
    ids = listed(payload)
    assert ("decision", decision.id) in ids
    assert ("capture", str(raw.id)) not in ids
    counts = person(payload, team.bob)["counts"]
    assert (counts["decisions"], counts["captures"]) == (1, 1)


async def test_projection_of_a_deleted_capture_is_not_counted(seeded) -> None:
    team = seeded
    source = capture(
        "Source capture later deleted",
        principal=team.bob,
        org=team.org,
        scope="project",
        project=team.shared,
        deleted_at=NOW.replace(tzinfo=None),
    )
    await surreal_content.save_raw_capture_record(None, capture=source)
    projected = entity(
        EntityType.DECISION,
        "Projection of a deleted capture",
        author=team.bob,
        project=team.shared,
        scope="project",
        metadata={"raw_memory_id": str(source.id)},
    )
    graph = await get_surreal_graph_runtime(team.org)
    await graph.entity_manager.create_direct_bulk([projected])

    payload = (await read_activity(team, team.alice)).json()
    assert projected.id not in json.dumps(payload)
    assert person(payload, team.bob)["counts"]["decisions"] == 0


async def test_an_organization_with_no_activity_lists_its_members(team) -> None:
    response = await read_activity(team, team.alice)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["recent"] == []
    assert payload["truncated"] is False
    assert {row["user_id"] for row in payload["people"]} == {team.alice, team.bob, team.carol}
    assert all(set(row["counts"].values()) == {0} for row in payload["people"])
    # Sorted by name when nobody has done anything.
    assert [row["name"] for row in payload["people"]] == ["Alice", "Bob", "Carol"]


async def test_another_organizations_rows_never_appear(seeded) -> None:
    team = seeded
    other_org = str(uuid4())
    await team.auth.execute_query(
        "CREATE organizations CONTENT $record;",
        record={"uuid": other_org, "name": "Elsewhere", "slug": f"elsewhere-{other_org[:8]}"},
    )
    await team.auth.execute_query(
        "CREATE organization_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": other_org,
            "user_id": team.bob,
            "role": "member",
        },
    )
    payload = (await read_activity(team, team.bob, org=other_org)).json()
    assert payload["recent"] == []
    assert [row["user_id"] for row in payload["people"]] == [team.bob]


@pytest.fixture(params=["embedded", "live"])
async def plan_clients(request) -> AsyncIterator[tuple[SurrealGraphClient, SurrealContentClient]]:
    """Clients for plan checks; the live pair needs SIBYL_LIVE_SURREAL_TESTS=1."""
    url = engine_url(request.param)
    group_id = f"activity-plan-{uuid4().hex[:10]}"
    graph = SurrealGraphClient(
        group_id=group_id, url=url, namespace_prefix="verify_", database="graph", **CREDENTIALS
    )
    content = SurrealContentClient(
        url=url, namespace=f"verify_content_{group_id.replace('-', '_')}", **CREDENTIALS
    )
    try:
        await prepare_graph_schema(graph)
        await bootstrap_content_schema(content)
        yield graph, content
    finally:
        if request.param == "live":
            await graph.execute_query(f"REMOVE NAMESPACE IF EXISTS {graph.namespace};")
            await content.execute_query(f"REMOVE NAMESPACE IF EXISTS {content.namespace};")
        await graph.close()
        await content.close()


async def test_window_reads_walk_their_indexes(plan_clients) -> None:
    """On a freshly bootstrapped schema, every window read is index served."""
    graph, content = plan_clients
    org = graph.group_id
    # The raw read names its index, so the index must ship with a fresh schema.
    info = json.dumps(await content.execute_query("INFO FOR TABLE raw_captures;"), default=str)
    assert "idx_raw_captures_org_created" in info
    await graph.execute_query(
        "INSERT INTO entity $rows;",
        rows=[
            {
                "uuid": f"plan-{index:03d}",
                "name": f"row {index}",
                "entity_type": ["task", "decision", "passage", "project"][index % 4],
                "group_id": org,
                "status": "done" if index % 2 else "todo",
                "created_at": NOW - timedelta(days=index % 40),
                "updated_at": NOW - timedelta(days=index % 40),
                "attributes": {},
            }
            for index in range(120)
        ],
    )
    await content.execute_query(
        "INSERT INTO raw_captures $rows;",
        rows=[
            {
                "uuid": str(uuid4()),
                "organization_id": org,
                "principal_id": "someone",
                "title": "plan",
                "created_at": NOW - timedelta(days=index % 40),
            }
            for index in range(120)
        ],
    )
    scope = activity.ReaderScope(
        user_id="someone",
        accessible_projects=set(),
        graph_projects=set(),
        project_ids=[],
        real_project_ids=[],
        has_unassigned=True,
        memory_grants=None,
        accessible_teams=set(),
        accessible_delegations=set(),
        project_filter=None,
    )
    private, private_params = activity.private_row_clause(scope)
    assert "$private_memory_owner" in private
    params = {
        "group_id": org,
        "organization_id": org,
        "since": NOW - timedelta(days=7),
        "until": NOW,
        "private_owner": "someone",
        "derived_types": sorted(activity.DERIVED_ENTITY_TYPES),
        **private_params,
    }
    checks = (
        (graph, activity.CREATED_IN_WINDOW + private, "idx_entity_updated"),
        (graph, activity.COMPLETED_IN_WINDOW + private, "idx_entity_type_status_updated"),
        # Any index led by entity_type serves the project-name equality.
        (graph, activity.PROJECT_NAMES, "idx_entity_type"),
        (
            content,
            surreal_content.RAW_ACTIVITY_STATEMENT + surreal_content._RAW_ACTIVITY_OWN_PRIVATE_ONLY,
            "idx_raw_captures_org_created",
        ),
    )
    for client, statement, index in checks:
        plan = await client.execute_query(statement + " EXPLAIN FULL;", **params)
        text = json.dumps(plan, default=str)
        assert index in text, (statement, text[:600])
        assert "TableScan" not in text, (statement, text[:600])
        assert "Iterate Table" not in text, (statement, text[:600])
