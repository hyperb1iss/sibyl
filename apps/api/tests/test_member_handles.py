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
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Request

from sibyl.api.routes import (
    auth as auth_routes,
    org_members as org_member_routes,
    tasks as task_routes,
)
from sibyl.auth.dependencies import get_current_user
from sibyl.persistence.surreal.member_handles import create_membership
from sibyl_core.auth.handles import (
    closest_handles,
    handle_base,
    normalize_handle,
    pick_handle,
)
from sibyl_core.backends.surreal.member_handles import backfill_member_handles
from sibyl_core.backends.surreal.records import normalize_records
from tests.test_routes_net import (
    client_for,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
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
    assert pick_handle("me", set()) == "me-2"
    assert len(pick_handle("x" * 64, {"x" * 64})) == 64


def test_handles_are_validated_like_channels() -> None:
    assert normalize_handle(" @Ada ") == "ada"
    for bad in ("", "-ada", "a b", "x" * 65, "me", "@me"):
        with pytest.raises(ValueError):
            normalize_handle(bad)
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


async def test_a_member_renames_themselves_and_owners_rename_anyone(
    world: SimpleNamespace,
) -> None:
    async def rename(actor: str, target: str, handle: str) -> Any:
        async with client_for(world, actor) as client:
            return await client.put(
                f"/api/orgs/{world.slug}/members/{target}/handle", json={"handle": handle}
            )

    mine = await rename(world.ada, world.ada, "@Ada.L")
    assert mine.status_code == 200, mine.text
    assert mine.json() == {"user_id": world.ada, "handle": "ada.l", "inbox": "@ada.l"}
    assert (await rename(world.ada, world.grace, "gracie")).status_code == 403
    assert (await rename(world.owen, world.grace, "gracie")).status_code == 200
    taken = await rename(world.vic, world.vic, "gracie")
    assert taken.status_code == 409
    assert taken.json()["detail"]["error"] == "handle_taken"
    assert (await rename(world.vic, world.vic, "me")).status_code == 422
    assert (await rename(world.vic, world.vic, "not valid")).status_code == 422
