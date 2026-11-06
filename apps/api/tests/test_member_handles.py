"""Member handles: every organization member holds one, unique in the organization.

A handle starts as the member's email local part, suffixed in join order on a
collision, is assigned under a per-organization lock whichever path creates
the membership, is backfilled for members that predate it, and shows on
``/auth/me`` and the members list. Runs with real auth, content and graph
stores on the embedded engine and, with SIBYL_LIVE_SURREAL_TESTS=1, on a live
server.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Request

from sibyl.api.routes import (
    auth as auth_routes,
    org_members as org_member_routes,
    tasks as task_routes,
)
from sibyl.auth.dependencies import get_current_user
from sibyl.persistence.surreal import member_handles
from sibyl.persistence.surreal.member_handles import create_membership, first_member_handle
from sibyl_core.auth.handles import closest_handles, handle_base, pick_handle
from sibyl_core.backends.surreal.member_handles import backfill_member_handles
from sibyl_core.backends.surreal.records import normalize_records
from tests.test_coordination_presence import (
    live_redis_settings as live_redis_settings,  # noqa: PLC0414
)
from tests.test_routes_net import (
    client_for,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
)

# --- the rules -------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "name", "expected"),
    [
        ("Ada.Lovelace@example.test", None, "ada.lovelace"),
        ("ada+agents@example.test", None, "ada-agents"),
        ("--ada@example.test", None, "ada"),
        (None, "Review Bot", "review-bot"),
        ("@example.test", "!!!", "member"),
        ("x" * 80 + "@example.test", None, "x" * 64),
    ],
)
def test_a_handle_starts_from_the_email_local_part(
    email: str | None, name: str | None, expected: str
) -> None:
    assert handle_base(email, name) == expected


def test_taken_and_reserved_handles_get_the_next_suffix() -> None:
    assert pick_handle("ada", set()) == "ada"
    assert pick_handle("ada", {"ada"}) == "ada-2"
    assert pick_handle("ada", {"ada", "ada-2"}) == "ada-3"
    for reserved in ("me", "all", "everyone", "here"):
        assert pick_handle(reserved, set()) == f"{reserved}-2"
    assert len(pick_handle("x" * 64, {"x" * 64})) == 64


def test_close_handles_are_suggested_best_first() -> None:
    assert closest_handles("adda", ["ada", "grace", "adam", "vic"]) == ["ada", "adam"]


# --- assignment ------------------------------------------------------------


async def member_rows(org: SimpleNamespace, organization: str | None = None) -> dict[str, str]:
    rows = normalize_records(
        await org.auth.execute_query(
            "SELECT user_id, handle FROM organization_members WHERE organization_id = $org;",
            org=organization or org.id,
        )
    )
    return {str(row["user_id"]): str(row.get("handle")) for row in rows}


async def new_user(org: SimpleNamespace, email: str | None, name: str | None = None) -> str:
    user = str(uuid4())
    await org.auth.execute_query(
        "CREATE users CONTENT $record;",
        record={"uuid": user, "email": email, "name": name or "Someone"},
    )
    return user


async def join(org: SimpleNamespace, user: str, organization: str | None = None) -> None:
    await create_membership(
        org.auth,
        {
            "uuid": str(uuid4()),
            "organization_id": organization or org.id,
            "user_id": user,
            "role": "member",
        },
    )


async def test_new_members_get_their_local_part_suffixed_in_join_order(
    org: SimpleNamespace,
) -> None:
    tag = uuid4().hex[:6]
    first = await new_user(org, f"lin{tag}@example.test")
    second = await new_user(org, f"lin{tag}@elsewhere.test")
    racers = [await new_user(org, f"kim{tag}@x{index}.test") for index in range(4)]

    await join(org, first)
    await join(org, second)
    await asyncio.gather(*(join(org, racer) for racer in racers))

    handles = await member_rows(org)
    assert handles[first] == f"lin{tag}"
    assert handles[second] == f"lin{tag}-2"
    assert sorted(handles[racer] for racer in racers) == sorted(
        [f"kim{tag}", f"kim{tag}-2", f"kim{tag}-3", f"kim{tag}-4"]
    )
    # Handles are per organization: the same person starts over elsewhere.
    await join(org, second, org.other_org)
    assert (await member_rows(org, org.other_org))[second] == f"lin{tag}"


async def test_a_bot_member_gets_a_handle_from_its_name(org: SimpleNamespace) -> None:
    from sibyl.persistence.surreal.auth_runtime.bots import create_bot

    resolver_ctx = SimpleNamespace(
        organization=SimpleNamespace(id=org.id),
        org_role="owner",
        user=SimpleNamespace(id=org.owen, name="Owen", email=None),
    )
    bot = await create_bot(ctx=resolver_ctx, name="Review Bot", request=None)  # type: ignore[arg-type]

    assert (await member_rows(org))[str(bot["id"])] == "review-bot"


async def test_the_backfill_fills_missing_and_duplicate_handles_once(
    org: SimpleNamespace,
) -> None:
    organization = str(uuid4())
    tag = uuid4().hex[:6]
    users = [
        await new_user(org, f"sam{tag}@example.test"),
        await new_user(org, f"sam{tag}@other.test"),
        await new_user(org, None, "Sam Bot"),
        await new_user(org, f"dee{tag}@example.test"),
        await new_user(org, f"kit{tag}@example.test"),
    ]
    stamps = ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"]
    for index, user in enumerate(users[:3]):
        await org.auth.execute_query(
            "CREATE organization_members CONTENT { uuid: $uuid, organization_id: $org, "
            "user_id: $user, role: 'member', created_at: <datetime> $at };",
            uuid=str(uuid4()),
            org=organization,
            user=user,
            at=stamps[index],
        )
    # A merge brought two members holding the same handle into one organization.
    for user, at in ((users[3], "2026-01-04T00:00:00Z"), (users[4], "2026-01-05T00:00:00Z")):
        await org.auth.execute_query(
            "CREATE organization_members CONTENT { uuid: $uuid, organization_id: $org, "
            "user_id: $user, role: 'member', handle: $handle, created_at: <datetime> $at };",
            uuid=str(uuid4()),
            org=organization,
            user=user,
            handle=f"dee{tag}",
            at=at,
        )

    assert await backfill_member_handles(org.auth.execute_query, organization_id=organization) == 4
    handles = await member_rows(org, organization)
    assert [handles[user] for user in users] == [
        f"sam{tag}",
        f"sam{tag}-2",
        "sam-bot",
        f"dee{tag}",
        f"kit{tag}",
    ]
    assert await backfill_member_handles(org.auth.execute_query, organization_id=organization) == 0


def test_the_handle_migration_backfills_existing_members() -> None:
    from sibyl_core.backends.surreal.auth_schema import (
        AUTH_SCHEMA_CURRENT_VERSION,
        AUTH_SCHEMA_MIGRATIONS,
    )

    [migration] = [item for item in AUTH_SCHEMA_MIGRATIONS if item.name == "auth_member_handles"]
    assert migration.version == AUTH_SCHEMA_CURRENT_VERSION
    assert migration.action is not None
    assert any("FIELDS organization_id, handle" in stmt for stmt in migration.statements)


# --- discovery -------------------------------------------------------------


@pytest.fixture
async def world(org: SimpleNamespace) -> SimpleNamespace:
    org.app.include_router(auth_routes.router, prefix="/api")
    org.app.include_router(org_member_routes.router, prefix="/api")
    org.app.include_router(task_routes.router, prefix="/api")

    # The fixture authenticates by resolving each token into the request's
    # auth context; the members routes read their user from that context too.
    def current_user(request: Request) -> object:
        return request.state.auth_context.user

    org.app.dependency_overrides[get_current_user] = current_user
    return org


async def test_me_and_the_members_list_carry_the_handle(world: SimpleNamespace) -> None:
    async with client_for(world, world.ada) as client:
        me = (await client.get("/api/auth/me")).json()
        members = (await client.get(f"/api/orgs/{world.slug}/members")).json()["members"]

    assert me["handle"] == "ada"
    assert me["inbox"] == "@ada"
    assert {row["user"]["id"]: row["handle"] for row in members} == {
        world.ada: "ada",
        world.grace: "grace",
        world.vic: "vic",
        world.owen: "owen",
    }


async def test_reserved_handles_are_never_assigned_even_to_a_new_org_s_owner(
    org: SimpleNamespace,
) -> None:
    founder = await new_user(org, "me@example.test")
    assert await first_member_handle(org.auth, founder) == "me-2"
    joiners = [await new_user(org, f"{name}@example.test") for name in ("all", "everyone", "here")]
    for joiner in joiners:
        await join(org, joiner)

    handles = await member_rows(org)
    assert [handles[joiner] for joiner in joiners] == ["all-2", "everyone-2", "here-2"]


async def test_a_member_written_without_a_handle_gets_one_when_it_is_needed(
    world: SimpleNamespace,
) -> None:
    """A replica from before handles, mid rolling deploy, writes a bare membership."""
    tag = uuid4().hex[:6]
    late = await new_user(world, f"rolling{tag}@example.test")
    await world.auth.execute_query(
        "CREATE organization_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": world.id,
            "user_id": late,
            "role": "member",
        },
    )

    async with client_for(world, late) as client:
        me = (await client.get("/api/auth/me")).json()
    assert me["handle"] == f"rolling{tag}"
    assert me["inbox"] == f"@rolling{tag}"
    assert (await member_rows(world))[late] == f"rolling{tag}"


async def test_the_directory_and_inbox_lookups_heal_bare_memberships(
    world: SimpleNamespace,
) -> None:
    tag = uuid4().hex[:6]
    bare = await new_user(world, f"bare{tag}@example.test")
    await world.auth.execute_query(
        "CREATE organization_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": world.id,
            "user_id": bare,
            "role": "member",
        },
    )
    posted = await post(world, world.ada, channel=f"@bare{tag}", body="found you")
    assert posted.status_code == 201, posted.text

    other = await new_user(world, f"dir{tag}@example.test")
    await world.auth.execute_query(
        "CREATE organization_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": world.id,
            "user_id": other,
            "role": "member",
        },
    )
    async with client_for(world, world.vic) as client:
        directory = (await client.get("/api/net/directory")).json()["members"]
    assert {row["user_id"]: row["handle"] for row in directory}[other] == f"dir{tag}"


async def test_members_joining_through_two_replicas_take_distinct_handles(
    org: SimpleNamespace,
    live_redis_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handle lock is the Redis entity lock, so replicas serialize on it."""
    from sibyl.coordination._redis.locks import EntityLockManager

    replicas = [EntityLockManager(), EntityLockManager()]
    calls = iter(range(10_000))

    def lock_manager() -> EntityLockManager:
        return replicas[next(calls) % 2]

    monkeypatch.setattr("sibyl.locks.get_lock_manager", lock_manager)
    tag = uuid4().hex[:6]
    racers = [await new_user(org, f"race{tag}@x{index}.test") for index in range(6)]
    try:
        await asyncio.gather(*(join(org, racer) for racer in racers))
    finally:
        for replica in replicas:
            await replica.disconnect()

    handles = await member_rows(org)
    assert sorted(handles[racer] for racer in racers) == sorted(
        [f"race{tag}", *(f"race{tag}-{number}" for number in range(2, 7))]
    )
    assert member_handles.HANDLES_LOCK_ENTITY == "member-handles"
