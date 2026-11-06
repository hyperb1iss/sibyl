"""Finding people over sibylnet: the directory, the @-inbox rule and ``@me``.

``@<handle>`` is a member's inbox, ``@me`` the caller's own wherever a
channel is accepted, and an ``@`` channel nobody's handle owns is refused
with the closest handles instead of landing where nobody reads. Runs with
real auth, content and graph stores on the embedded engine and, with
SIBYL_LIVE_SURREAL_TESTS=1, on a live server.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.test_member_handles import world as world  # noqa: PLC0414
from tests.test_routes_net import (
    client_for,
    feed,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
    stored_messages,
)
from tests.test_routes_tasks_assign import assignees, create_task


async def test_the_directory_lists_every_member_with_who_is_listening(
    world: SimpleNamespace,
) -> None:
    for params in (
        {"channel": "@grace", "agent": "codex"},
        {"agent": "claude-code", "session": "s2"},
    ):
        assert (await feed(world, world.grace, limit=0, **params)).status_code == 200
    posted = await post(world, world.ada, channel="#reviews", body="hello")
    assert posted.status_code == 201, posted.text

    async with client_for(world, world.vic) as client:
        directory = (await client.get("/api/net/directory")).json()

    rows = {row["handle"]: row for row in directory["members"]}
    assert [row["handle"] for row in directory["members"]] == ["ada", "grace", "owen", "vic"]
    assert rows["grace"] == {
        "user_id": world.grace,
        "name": "Grace",
        "handle": "grace",
        "inbox": "@grace",
        "listening": 2,
        "last_post_at": None,
    }
    assert rows["ada"]["listening"] == 0
    assert rows["ada"]["last_post_at"] is not None


async def test_a_renamed_member_s_old_inbox_is_refused_and_the_new_one_works(
    world: SimpleNamespace,
) -> None:
    async with client_for(world, world.grace) as client:
        renamed = await client.put(
            f"/api/orgs/{world.slug}/members/{world.grace}/handle", json={"handle": "gracie"}
        )
    assert renamed.status_code == 200, renamed.text

    old = await post(world, world.owen, channel="@grace", body="hi")
    assert old.status_code == 422, old.text
    assert old.json()["detail"]["suggestions"] == ["@gracie"]
    assert (await post(world, world.owen, channel="@gracie", body="hi")).status_code == 201


# --- no void ---------------------------------------------------------------


async def test_an_inbox_nobody_owns_is_refused_with_the_closest_handles(
    world: SimpleNamespace,
) -> None:
    refused = await post(world, world.ada, channel="@grase", body="hello?")

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == {
        "error": "unknown_inbox",
        "message": "no member uses @grase; did you mean @grace?",
        "suggestions": ["@grace"],
    }
    nothing_close = await post(world, world.ada, channel="@zzzzzz", body="hello?")
    assert nothing_close.json()["detail"]["suggestions"] == []
    assert nothing_close.json()["detail"]["message"].endswith("see sibyl net who")
    for free in ("#anything", "anything"):
        assert (await post(world, world.ada, channel=free, body="hi")).status_code == 201
    assert len(await stored_messages(world)) == 2


async def test_an_assign_to_an_inbox_nobody_owns_writes_nothing(world: SimpleNamespace) -> None:
    task = await create_task(world, world.shared)
    async with client_for(world, world.ada) as client:
        refused = await client.post(f"/api/tasks/{task}/assign", json={"to": "@graec"})

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["suggestions"] == ["@grace"]
    assert await assignees(world, task) == []
    assert await stored_messages(world) == []


# --- @me -------------------------------------------------------------------


async def test_at_me_is_the_callers_own_inbox_everywhere(world: SimpleNamespace) -> None:
    posted = await post(world, world.grace, channel="@me", body="note to self", agent="codex")
    assert posted.status_code == 201, posted.text
    assert posted.json()["message"]["channel"] == "@grace"

    read = await feed(world, world.grace, channel="@me")
    assert read.status_code == 200, read.text
    async with client_for(world, world.grace) as client:
        presence = (await client.get("/api/net/presence", params={"channel": "@me"})).json()
    assert {row["channel"] for row in presence["watching"]} == {"@grace"}
    assert [row["channel"] for row in presence["recent"]] == ["@grace"]

    task = await create_task(world, world.shared)
    async with client_for(world, world.grace) as client:
        assigned = await client.post(f"/api/tasks/{task}/assign", json={"to": "@me"})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["message"]["channel"] == "@grace"
    assert await assignees(world, task) == ["@grace"]
