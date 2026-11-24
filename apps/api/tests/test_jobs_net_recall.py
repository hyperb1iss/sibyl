"""A quiet multi-author thread ends up as each author's own recallable memories.

End to end over the real routes, job and stores (embedded, and live when
SIBYL_LIVE_SURREAL_TESTS=1 names a server): two authors talk in a thread
pinned to a project, one of them through a key held to another project. The
distillation job runs directly with a small idle setting instead of waiting
half an hour. Each author's decision lines must come back as decision
memories attributed to that author, in the scope their own authority allows
(the pinned project for one, private for the other), and a context pack for
the subject must surface them to the author, and to nobody who cannot read
that scope.

The heuristic extractor does the reflecting, so nothing here calls a model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sibyl.api.routes import context as context_routes
from sibyl.config import settings as api_settings
from sibyl.jobs.net import distill_idle_net_threads
from sibyl_core.services.graph_runtime import get_surreal_graph_runtime
from sibyl_core.services.net import list_net_threads
from sibyl_core.tools import context as core_context, search as core_search, usage_exposure
from tests.test_jobs_net import reflections as reflections  # noqa: PLC0414
from tests.test_routes_net import (
    api_key,
    client_for,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
)

CHANNEL = "#reviews"
THREAD = "gh:acme/app#7"
ADA_DECISION = "Decided: we will use zebracursor keyset paging for the ledger feed."
GRACE_DECISION = "Decided: the quokkaqueue retries stay at the client."


@pytest.fixture
def recall_routes(org: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The context pack route on the same app, lexical lanes only (no embedder)."""
    org.app.include_router(context_routes.router, prefix="/api")
    monkeypatch.setattr(core_context, "configured_embedding_provider", lambda: None)
    monkeypatch.setattr(core_search, "configured_embedding_provider", lambda: None)

    async def content_scope():
        return org.content

    monkeypatch.setattr(usage_exposure, "get_shared_surreal_content_client", content_scope)
    return org


async def context_pack(org: SimpleNamespace, user: str, goal: str) -> list[dict[str, object]]:
    async with client_for(org, user) as client:
        response = await client.post(
            "/api/context/pack",
            json={"goal": goal, "include_related": False, "record_exposure": False, "limit": 50},
        )
    assert response.status_code == 200, response.text
    return pack_items(response.json())


def pack_items(pack: dict[str, object]) -> list[dict[str, object]]:
    return [item for section in pack["sections"] for item in section["items"]]  # type: ignore[union-attr]


async def test_each_authors_decisions_become_their_own_recallable_memories(
    recall_routes: SimpleNamespace,
    reflections: list[SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = recall_routes
    # Ada's agent posts through a key held to another project, so her part
    # cannot reach the pinned project even though she contributes to it.
    _, ada_key = await api_key(org, org.ada, projects=[org.other])
    conversation = [
        (org.grace, None, "Opening the paging question for the ledger feed.", org.shared),
        (org.ada, ada_key, ADA_DECISION, None),
        (org.grace, None, f"Agreed on paging. {GRACE_DECISION}", None),
        (org.ada, ada_key, "Thanks, that settles it.", None),
    ]
    for user, token, body, project in conversation:
        posted = await post(
            org,
            user,
            token=token,
            channel=CHANNEL,
            thread=THREAD,
            agent="claude-code",
            body=body,
            **({"project_id": project} if project else {}),
        )
        assert posted.status_code == 201, posted.text

    # A one-minute idle setting and two minutes of quiet, instead of thirty.
    monkeypatch.setattr(api_settings, "net_thread_idle_minutes", 1)
    await org.content.execute_query(
        "UPDATE net_messages SET created_at = created_at - 2m WHERE organization_id = $org;"
        "UPDATE net_threads SET last_message_at = last_message_at - 2m, "
        "first_message_at = first_message_at - 2m WHERE organization_id = $org;",
        org=org.id,
    )

    summary = await distill_idle_net_threads({})

    assert (summary["distilled"], summary["failed"], summary["authors_skipped"]) == (1, 0, 0)
    by_author = {call.principal_id: call for call in reflections}
    assert set(by_author) == {org.ada, org.grace}
    expectations = {
        org.ada: (ADA_DECISION, "zebracursor", ("private", None)),
        org.grace: (GRACE_DECISION, "quokkaqueue", ("project", org.shared)),
    }
    graph = await get_surreal_graph_runtime(org.id)
    # Each author's distilled memory: the transcript source and what it yielded.
    distilled: dict[str, set[str]] = {}
    for author, (line, token, (scope, project)) in expectations.items():
        call = by_author[author]
        other_line = expectations[org.grace if author == org.ada else org.ada][0]
        assert other_line not in call.transcript
        decisions = [
            candidate
            for candidate in call.pack.candidates
            if candidate.kind == "decision" and token in candidate.content
        ]
        assert decisions, [(c.kind, c.content) for c in call.pack.candidates]
        assert all(candidate.persisted_id for candidate in decisions)
        for candidate in call.pack.candidates:
            assert other_line not in candidate.content
        source = await graph.entity_manager.get(str(call.pack.source_id))
        assert source.name == f"sibylnet {CHANNEL} {THREAD}"
        assert source.metadata.get("principal_id") == author
        assert source.metadata.get("memory_scope") == scope
        assert line in (source.content or source.description)
        for candidate in decisions:
            entity = await graph.entity_manager.get(str(candidate.persisted_id))
            assert entity.entity_type == "decision"
            assert entity.metadata.get("principal_id") == author
            assert entity.metadata.get("memory_scope") == scope
            assert entity.metadata.get("project_id") == project
            # Every decision cites the conversation it came from.
            assert entity.metadata.get("raw_source_ids") == [str(call.pack.source_id)]
        distilled[author] = {str(call.pack.source_id)} | {
            str(candidate.persisted_id) for candidate in decisions
        }

    state = next(
        s for s in await list_net_threads(organization_id=org.id, channel=CHANNEL) if s.thread
    )
    assert state.project_id == org.shared
    assert state.reflected_through == state.last_message_at

    # Recall: each author's distilled memory surfaces for the subject, to
    # them; Ada, a contributor to the pinned project, also finds Grace's;
    # Grace cannot see Ada's private one. A pack may surface the decision or
    # the transcript it cites; either is that author's distilled memory.
    for reader, goal, author, phrase in (
        (org.ada, "zebracursor keyset paging", org.ada, "zebracursor"),
        (org.grace, "quokkaqueue retries", org.grace, "quokkaqueue"),
        (org.ada, "quokkaqueue retries", org.grace, "quokkaqueue"),
    ):
        items = await context_pack(org, reader, goal)
        assert any(
            str(item.get("id")) in distilled[author] and phrase in str(item) for item in items
        ), (sorted(distilled[author]), [(item.get("id"), item.get("type")) for item in items])
    leaked = await context_pack(org, org.grace, "zebracursor keyset paging")
    assert not any("zebracursor" in str(item) for item in leaked), leaked
    # A member of another organization finds neither.
    async with client_for(org, org.ozzy, org.other_org) as client:
        response = await client.post(
            "/api/context/pack",
            json={
                "goal": "quokkaqueue zebracursor",
                "include_related": False,
                "record_exposure": False,
            },
        )
    assert response.status_code == 200, response.text
    found = str(pack_items(response.json()))
    assert "quokkaqueue" not in found
    assert "zebracursor" not in found


async def test_a_decision_marked_in_the_channel_is_recallable_at_once(
    recall_routes: SimpleNamespace,
) -> None:
    org = recall_routes
    posted = await post(
        org,
        org.ada,
        channel="#decisions",
        thread="gh:acme/app#9",
        kind="decision",
        project_id=org.shared,
        body="Decided: the wombatwal checkpoint runs before every deploy.",
    )
    assert posted.status_code == 201, posted.text
    memory_id = posted.json()["decision_memory_id"]

    for reader in (org.ada, org.grace):
        items = await context_pack(org, reader, "wombatwal checkpoint deploy")
        assert any(memory_id in str(item.get("id")) for item in items), items
    async with client_for(org, org.vic) as client:
        response = await client.post(
            "/api/context/pack",
            json={
                "goal": "wombatwal checkpoint",
                "include_related": False,
                "record_exposure": False,
            },
        )
    # Vic reads the organization but not the private shared project.
    assert response.status_code == 200, response.text
    assert not any(memory_id in str(item) for item in pack_items(response.json()))
