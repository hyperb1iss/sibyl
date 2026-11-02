"""Idle sibylnet threads settle into memory as the author of their first message.

Runs against the same isolated stores as the route tests: messages are posted
over HTTP, aged past the idle threshold, and the scheduled job reflects them.
"""

from __future__ import annotations

from types import SimpleNamespace

from sibyl.jobs import worker as worker_module
from sibyl.jobs.net import distill_idle_net_threads
from sibyl_core.services.graph_runtime import get_surreal_graph_runtime
from sibyl_core.services.net import list_net_threads
from tests.test_routes_net import (
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
    stored_captures,
)


async def go_quiet(org: SimpleNamespace, minutes: int = 45) -> None:
    await org.content.execute_query(
        f"UPDATE net_messages SET created_at = created_at - {minutes}m "  # noqa: S608
        "WHERE organization_id = $org;"
        f"UPDATE net_threads SET last_message_at = last_message_at - {minutes}m, "
        f"first_message_at = first_message_at - {minutes}m WHERE organization_id = $org;",
        org=org.id,
    )


async def thread_row(org: SimpleNamespace, channel: str, thread: str):
    states = await list_net_threads(organization_id=org.id, channel=channel)
    return next(state for state in states if state.thread == thread)


def test_distillation_runs_every_five_minutes_on_both_schedulers() -> None:
    specs = {spec.name: spec for spec in worker_module.get_schedule_specs()}

    spec = specs["distill_idle_net_threads"]
    assert spec.minute == set(range(0, 60, 5))
    assert spec.hour is None
    assert distill_idle_net_threads in worker_module.WorkerSettings.functions


async def test_an_idle_thread_settles_into_its_first_authors_memory(
    org: SimpleNamespace,
) -> None:
    for user, body in (
        (org.ada, "Should the feed page by offset or keyset?"),
        (org.grace, "Keyset. Offset rescans the index on every page."),
        (org.ada, "Decided: we use keyset paging everywhere."),
    ):
        posted = await post(
            org,
            user,
            channel="#reviews",
            thread="gh:acme/app#42",
            project_id=org.shared,
            agent="claude-code",
            body=body,
        )
        assert posted.status_code == 201, posted.text
    await go_quiet(org)

    summary = await distill_idle_net_threads({})

    assert summary["distilled"] == 1, summary
    state = await thread_row(org, "#reviews", "gh:acme/app#42")
    assert state.reflection_error is None
    assert state.reflection_attempts == 0
    assert state.reflected_through == state.last_message_at
    assert state.reflection_source_id
    graph = await get_surreal_graph_runtime(org.id)
    source = await graph.entity_manager.get(state.reflection_source_id)
    assert source.name == "sibylnet #reviews gh:acme/app#42"
    transcript = source.content or source.description
    assert transcript.startswith("# sibylnet channel #reviews, thread gh:acme/app#42")
    assert "Ada (claude-code): Decided: we use keyset paging everywhere." in transcript
    assert "Grace (claude-code): Keyset." in transcript
    # Reflected as Ada, into the shared project's default write scope.
    assert source.metadata.get("principal_id") == org.ada
    assert source.metadata.get("memory_scope") == "project"
    assert source.metadata.get("scope_key") == org.shared

    again = await distill_idle_net_threads({})
    assert again["candidates"] == 0


async def test_a_threadless_message_without_a_project_stays_private(
    org: SimpleNamespace,
) -> None:
    assert (await post(org, org.grace, channel="@ada", body="Review my branch")).status_code == 201
    await go_quiet(org)

    summary = await distill_idle_net_threads({})

    assert summary["distilled"] == 1, summary
    state = await thread_row(org, "@ada", "")
    graph = await get_surreal_graph_runtime(org.id)
    source = await graph.entity_manager.get(str(state.reflection_source_id))
    assert source.name == "sibylnet @ada"
    assert source.metadata.get("principal_id") == org.grace
    assert source.metadata.get("memory_scope") == "private"


async def test_an_author_who_lost_write_access_fails_the_attempt(org: SimpleNamespace) -> None:
    assert (await post(org, org.ada, channel="#reviews", body="Ship it")).status_code == 201
    await go_quiet(org)
    await org.auth.execute_query(
        "UPDATE organization_members SET role = 'viewer' "
        "WHERE organization_id = $org AND user_id = $user;",
        org=org.id,
        user=org.ada,
    )

    summary = await distill_idle_net_threads({})

    assert summary["failed"] == 1, summary
    state = await thread_row(org, "#reviews", "")
    assert state.reflection_error == "principal_cannot_write"
    assert state.reflection_attempts == 1
    assert state.reflected_through is None
    assert await stored_captures(org) == []


async def test_an_author_who_left_the_project_fails_the_attempt(org: SimpleNamespace) -> None:
    posted = await post(
        org, org.grace, channel="#secret", body="Rotate the keys", project_id=org.secret
    )
    assert posted.status_code == 201, posted.text
    await go_quiet(org)
    await org.auth.execute_query("DELETE project_members WHERE user_id = $user;", user=org.grace)

    summary = await distill_idle_net_threads({})

    assert summary["failed"] == 1, summary
    state = await thread_row(org, "#secret", "")
    assert state.reflection_error is not None
    assert state.reflection_error.startswith("http_403")
    assert state.reflection_attempts == 1
