"""Handing a task to another agent: ``POST /tasks/{id}/assign`` and ``/unassign``.

The task stays the state and sibylnet carries the handoff: an assign adds the
channel to the task's assignees through the locked task update path, then
posts the handoff on the channel's ``task:<id>`` thread, pinned to the task's
project so the conversation is distilled into that project's memory. Runs
with real auth, content and graph stores on the embedded engine and, with
SIBYL_LIVE_SURREAL_TESTS=1, on a live server.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest

from sibyl.api.routes import tasks as task_routes
from sibyl.persistence.graph_runtime import get_task_graph_runtime
from sibyl_core.models.tasks import Task, TaskStatus
from sibyl_core.services.net import post_net_message
from tests.test_routes_net import (
    api_key,
    client_for,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
    stored_messages,
)

TITLE = "Fix the flaky fixture"


async def create_task(world: SimpleNamespace, project: str, **fields: Any) -> str:
    task = Task(
        id=str(uuid4()),
        name=TITLE,
        title=TITLE,
        status=TaskStatus.TODO,
        project_id=project,
        created_by=world.ada,
        **fields,
    )
    runtime = await get_task_graph_runtime(world.id)
    return await runtime.entity_manager.create_direct(task)


@pytest.fixture
async def world(org: SimpleNamespace) -> SimpleNamespace:
    org.app.include_router(task_routes.router, prefix="/api")
    org.task = await create_task(org, org.shared)
    return org


async def assign(
    world: SimpleNamespace,
    user: str,
    task_id: str | None = None,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    **body: object,
) -> httpx.Response:
    async with client_for(world, user, token=token) as client:
        return await client.post(
            f"/api/tasks/{task_id or world.task}/assign", json=body, headers=headers
        )


async def unassign(world: SimpleNamespace, user: str, **body: object) -> httpx.Response:
    async with client_for(world, user) as client:
        return await client.post(f"/api/tasks/{world.task}/unassign", json=body)


async def assignees(world: SimpleNamespace, task_id: str | None = None) -> list[str]:
    runtime = await get_task_graph_runtime(world.id)
    task = await runtime.entity_manager.get(task_id or world.task)
    return list(task.metadata.get("assignees") or [])


async def test_assign_adds_the_assignee_and_posts_the_handoff_pinned_to_the_project(
    world: SimpleNamespace,
) -> None:
    response = await assign(
        world,
        world.ada,
        to=" @Grace ",
        note="The fixture fix is on the branch.\nIt needs the migration review.",
        agent="claude-code",
        session="session-1",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True
    assert body["task"]["id"] == world.task
    assert body["task"]["metadata"]["assignees"] == ["@grace"]
    message = body["message"]
    assert message["channel"] == "@grace"
    assert message["thread"] == f"task:{world.task}"
    assert message["project_id"] == world.shared
    assert message["kind"] == "message"
    assert message["body"] == (
        f"Assigned: {TITLE}\n"
        "The fixture fix is on the branch.\nIt needs the migration review.\n"
        f"Task: {world.task}"
    )
    assert message["author"]["user_id"] == world.ada
    assert message["author"]["name"] == "Ada"
    assert message["author"]["agent"] == "claude-code"
    assert message["author"]["session"] == "session-1"
    assert message["api_key_id"] is None
    receipt = body["mutation_receipt"]
    assert receipt["applied"] is True
    assert receipt["affected_records"] == [f"entity:{world.task}", f"net_message:{message['id']}"]
    assert receipt["revision"] == body["task"]["revision"]

    # The task is the state: written through the update path, by the caller.
    runtime = await get_task_graph_runtime(world.id)
    stored = await runtime.entity_manager.get(world.task)
    assert stored.metadata["assignees"] == ["@grace"]
    assert stored.modified_by == world.ada
    assert stored.revision == body["task"]["revision"] > 1

    # The thread is pinned to the task's project, so it distills there.
    async with client_for(world, world.grace) as client:
        threads = (await client.get("/api/net/threads", params={"channel": "@grace"})).json()
    assert [(row["thread"], row["project_id"]) for row in threads["threads"]] == [
        (f"task:{world.task}", world.shared)
    ]


async def test_assigning_again_keeps_one_assignee_and_posts_the_handoff_again(
    world: SimpleNamespace,
) -> None:
    first = await assign(world, world.ada, to="@grace")
    again = await assign(world, world.ada, to="@GRACE", note="Still needs eyes.")

    assert first.status_code == again.status_code == 200, again.text
    assert again.json()["changed"] is False
    assert again.json()["task"]["revision"] == first.json()["task"]["revision"]
    assert again.json()["mutation_receipt"]["affected_records"] == [
        f"net_message:{again.json()['message']['id']}"
    ]
    assert await assignees(world) == ["@grace"]
    bodies = sorted(str(row["body"]) for row in await stored_messages(world))
    assert bodies == [
        f"Assigned: {TITLE}\nStill needs eyes.\nTask: {world.task}",
        f"Assigned: {TITLE}\nTask: {world.task}",
    ]


async def test_a_retried_assign_posts_once(world: SimpleNamespace) -> None:
    key = {"Idempotency-Key": f"assign-{uuid4()}"}
    first = await assign(world, world.ada, headers=key, to="@grace", note="Over to you")
    retried = await assign(world, world.ada, headers=key, to="@grace", note="Over to you")

    assert first.status_code == retried.status_code == 200, retried.text
    assert retried.json()["message"] == first.json()["message"]
    assert retried.json()["task"] == first.json()["task"]
    assert retried.json()["mutation_receipt"]["replayed"] is True
    assert len(await stored_messages(world)) == 1
    assert await assignees(world) == ["@grace"]

    reused = await assign(world, world.ada, headers=key, to="@grace", note="Different")
    assert reused.status_code == 409, reused.text
    assert len(await stored_messages(world)) == 1


async def test_unassign_removes_the_assignee_and_posts_one_line_on_the_same_thread(
    world: SimpleNamespace,
) -> None:
    other = await create_task(world, world.shared, assignees=["Bob"])
    world.task = other
    assert (await assign(world, world.ada, to="@grace")).status_code == 200
    assert await assignees(world) == ["Bob", "@grace"]

    removed = await unassign(world, world.grace, assignee="@Grace", agent="codex")

    assert removed.status_code == 200, removed.text
    body = removed.json()
    assert body["changed"] is True
    assert body["task"]["metadata"]["assignees"] == ["Bob"]
    assert body["message"]["body"] == f"Unassigned: {TITLE} (task {other})"
    assert body["message"]["thread"] == f"task:{other}"
    assert body["message"]["channel"] == "@grace"
    assert body["message"]["author"]["user_id"] == world.grace
    assert await assignees(world) == ["Bob"]

    again = await unassign(world, world.grace, assignee="@grace")
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False
    assert again.json()["message"] is None
    assert again.json()["mutation_receipt"]["applied"] is False
    assert len(await stored_messages(world)) == 2


async def threads_of(world: SimpleNamespace, channel: str) -> list[tuple[object, object]]:
    async with client_for(world, world.grace) as client:
        listing = (await client.get("/api/net/threads", params={"channel": channel})).json()
    return [(row["thread"], row["project_id"]) for row in listing["threads"]]


async def test_a_task_thread_belongs_to_its_task_whoever_posts_first(
    world: SimpleNamespace,
) -> None:
    thread = f"task:{world.task}"
    # Claiming the handoff thread for another project ahead of the handoff
    # is refused; replying in it, with or without the task's project, is not.
    squat = await post(
        world, world.ada, channel="@grace", body="mine now", thread=thread, project_id=world.other
    )
    assert squat.status_code == 409, squat.text
    assert squat.json()["detail"]["project_id"] == world.shared
    for project in (None, world.shared):
        reply = await post(
            world,
            world.grace,
            channel="@grace",
            body="On it",
            thread=thread,
            **({"project_id": project} if project else {}),
        )
        assert reply.status_code == 201, reply.text
    assert await threads_of(world, "@grace") == [(thread, world.shared)]

    assigned = await assign(world, world.ada, to="@grace")
    assert assigned.status_code == 200, assigned.text
    assert await threads_of(world, "@grace") == [(thread, world.shared)]


async def test_a_pin_stored_for_another_project_never_blocks_the_handoff(
    world: SimpleNamespace,
) -> None:
    """A squatted pin, or one racing the assign, gives way to the task's project."""
    thread = f"task:{world.task}"
    # A pin stored before task threads followed their task, or by a direct writer.
    await post_net_message(
        organization_id=world.id,
        channel="@grace",
        thread=thread,
        kind="message",
        body="An older conversation",
        author_user_id=world.ada,
        project_id=world.other,
    )

    async def squat() -> int:
        response = await post(
            world, world.ada, channel="@grace", body="x", thread=thread, project_id=world.other
        )
        return response.status_code

    outcomes = await asyncio.gather(assign(world, world.ada, to="@grace"), squat(), squat())

    assert outcomes[0].status_code == 200, outcomes[0].text
    assert outcomes[1:] == [409, 409]
    assert await assignees(world) == ["@grace"]
    assert await threads_of(world, "@grace") == [(thread, world.shared)]
    assert len(await stored_messages(world)) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"to": "not a channel"},
        {"to": "@" + "a" * 65},
        {"to": "@grace", "note": "x" * 16_001},
        {"to": "@grace", "agent": "two\nlines"},
        {},
    ],
)
async def test_bad_input_is_refused_before_anything_is_written(
    world: SimpleNamespace, body: dict[str, object]
) -> None:
    refused = await assign(world, world.ada, **body)

    assert refused.status_code == 422, refused.text
    assert await assignees(world) == []
    assert await stored_messages(world) == []


async def test_who_may_assign(world: SimpleNamespace) -> None:
    """Write access to the task, inside the API key's project grants."""
    readonly_task = await create_task(world, world.readonly)
    secret_task = await create_task(world, world.secret)
    shared_key_id, shared_key = await api_key(world, world.ada, projects=[world.shared])
    _, other_key = await api_key(world, world.ada, projects=[world.other])
    open_key_id, open_key = await api_key(world, world.ada, projects=None)
    cases = [
        ("member contributor, session", world.ada, None, world.task, 200),
        ("organization owner without project membership", world.owen, None, world.task, 200),
        ("key held to the task's project", world.ada, shared_key, world.task, 200),
        ("unrestricted key", world.ada, open_key, world.task, 200),
        ("key held to another project", world.ada, other_key, world.task, 403),
        ("organization viewer", world.vic, None, world.task, 403),
        ("project viewer", world.ada, None, readonly_task, 403),
        ("no access to the project", world.ada, None, secret_task, 403),
    ]
    outcomes = {}
    for label, user, token, task_id, _ in cases:
        response = await assign(world, user, task_id, token=token, to="@grace")
        outcomes[label] = response.status_code
    assert outcomes == {label: status for label, _, _, _, status in cases}

    async with client_for(world, world.ozzy, world.other_org) as client:
        elsewhere = await client.post(f"/api/tasks/{world.task}/assign", json={"to": "@grace"})
    assert elsewhere.status_code == 404, elsewhere.text

    # Only the allowed handoffs posted, each stamped with the key it used.
    messages = await stored_messages(world)
    assert sorted(str(row.get("api_key_id")) for row in messages) == sorted(
        ["None", "None", shared_key_id, open_key_id]
    )
    assert await assignees(world, readonly_task) == []
    assert await assignees(world, secret_task) == []
