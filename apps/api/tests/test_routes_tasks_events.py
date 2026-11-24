"""A task's transitions reach the channels it was handed to, as events.

The task stays the state. When it starts, blocks, goes to review, completes
or is archived, the server posts one ``event`` on the task's ``task:<id>``
thread in each ``@inbox`` or ``#room`` among its assignees, so whoever handed
it off hears without polling the task. Runs with real auth, content and graph
stores on the embedded engine and, with SIBYL_LIVE_SURREAL_TESTS=1, on a live
server.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from sibyl.api.routes import tasks as task_routes
from tests.test_routes_net import (
    client_for,
    feed,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
    settle,
    stored_messages,
)
from tests.test_routes_tasks_assign import (
    TITLE,
    assign,
    create_task,
    world as world,  # noqa: PLC0414
)


async def act(
    world: SimpleNamespace,
    user: str,
    action: str,
    task_id: str | None = None,
    **body: object,
) -> httpx.Response:
    async with client_for(world, user) as client:
        return await client.post(f"/api/tasks/{task_id or world.task}/{action}", json=body or None)


async def events(world: SimpleNamespace) -> list[dict[str, object]]:
    rows = [row for row in await stored_messages(world) if row.get("kind") == "event"]
    return sorted(rows, key=lambda row: int(str(row["created_ns"])))


async def test_each_transition_posts_one_event_on_the_task_thread(
    world: SimpleNamespace,
) -> None:
    assert (await assign(world, world.ada, to="@grace")).status_code == 200
    about = f"{TITLE} (task {world.task})"
    steps: list[tuple[str, dict[str, object], str]] = [
        ("start", {}, f"Started: {about}"),
        (
            "block",
            {"reason": "Waiting on the schema review."},
            f"Blocked: {about}\nWaiting on the schema review.",
        ),
        ("unblock", {}, f"Unblocked: {about}"),
        (
            "review",
            {"pr_url": "https://example.test/acme/app/pull/42"},
            f"In review: {about}\nhttps://example.test/acme/app/pull/42",
        ),
        ("complete", {}, f"Completed: {about}"),
        ("archive", {"reason": "Shipped."}, f"Archived: {about}\nShipped."),
    ]

    for action, body, _ in steps:
        response = await act(world, world.grace, action, **body)
        assert response.status_code == 200, (action, response.text)
        affected = response.json()["mutation_receipt"]["affected_records"]
        assert affected[0] == f"entity:{world.task}", action
        assert [record.split(":")[0] for record in affected] == ["entity", "net_message"], action

    posted = await events(world)
    assert [row["body"] for row in posted] == [body for _, _, body in steps]
    for row in posted:
        assert row["channel"] == "@grace"
        assert row["thread"] == f"task:{world.task}"
        # Pinned to the task's project and posted as whoever made the change.
        assert row["project_id"] == world.shared
        assert row["author_user_id"] == world.grace


async def test_an_event_reads_as_one_and_only_the_server_writes_them(
    world: SimpleNamespace,
) -> None:
    assert (await assign(world, world.ada, to="@grace")).status_code == 200
    assert (await act(world, world.grace, "start")).status_code == 200
    await settle(world)

    page = await feed(world, world.grace, channel="@grace")

    assert page.status_code == 200, page.text
    assert [(row["kind"], row["body"].split(":")[0]) for row in page.json()["messages"]] == [
        ("message", "Assigned"),
        ("event", "Started"),
    ]

    refused = await post(
        world, world.ada, channel="@grace", body="Completed: all of it", kind="event"
    )
    assert refused.status_code == 422, refused.text
    assert len(await events(world)) == 1


async def test_only_inboxes_and_rooms_among_the_assignees_hear_of_it(
    world: SimpleNamespace,
) -> None:
    # A free-text name from before handoffs, a bare name that would pass as a
    # channel, an inbox twice over, a room, and an inbox nobody holds.
    handed = await create_task(
        world,
        world.shared,
        assignees=["Bob", "reviews", "@grace", "#Reviews", "@GRACE", "@nobody"],
    )
    unhanded = await create_task(world, world.shared, assignees=["Bob"])

    assert (await act(world, world.ada, "start", handed)).status_code == 200
    quiet = await act(world, world.ada, "start", unhanded)

    assert quiet.status_code == 200, quiet.text
    assert quiet.json()["mutation_receipt"]["affected_records"] == [f"entity:{unhanded}"]
    posted = await events(world)
    assert sorted(str(row["channel"]) for row in posted) == ["#reviews", "@grace"]
    assert {row["thread"] for row in posted} == {f"task:{handed}"}


async def test_a_transition_that_changes_nothing_posts_nothing(
    world: SimpleNamespace,
) -> None:
    assert (await assign(world, world.ada, to="@grace")).status_code == 200

    for _ in range(3):
        again = await act(world, world.grace, "start")
        assert again.status_code == 200, again.text

    # Starting a started task is accepted and moves nothing, so it wakes nobody.
    assert again.json()["mutation_receipt"]["affected_records"] == [f"entity:{world.task}"]
    assert [row["body"] for row in await events(world)] == [f"Started: {TITLE} (task {world.task})"]


async def test_a_thread_stays_caught_up_when_only_events_follow(
    world: SimpleNamespace,
) -> None:
    assert (await assign(world, world.ada, to="@grace")).status_code == 200
    # Stand in for a finished distillation: nothing said is waiting.
    await world.content.execute_query(
        "UPDATE net_threads SET reflection_pending = false, "
        "reflected_through = last_message_at WHERE organization_id = $org;",
        org=world.id,
    )

    assert (await act(world, world.grace, "start")).status_code == 200

    async with client_for(world, world.grace) as client:
        listing = (await client.get("/api/net/threads", params={"channel": "@grace"})).json()
    (row,) = listing["threads"]
    assert row["message_count"] == 2
    # The event moved the thread's last activity past the watermark, and the
    # thread still has nothing to distill.
    assert row["last_message_at"] > row["reflected_through"]
    assert row["reflection_pending"] is False


async def test_a_notice_that_cannot_be_posted_never_fails_the_transition(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await assign(world, world.ada, to="@grace")).status_code == 200

    async def away(**_: object) -> None:
        raise RuntimeError("the content store is away")

    monkeypatch.setattr(task_routes, "deliver_net_message", away)

    response = await act(world, world.grace, "start")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["status"] == "doing"
    assert body["mutation_receipt"]["affected_records"] == [f"entity:{world.task}"]
    assert await events(world) == []
