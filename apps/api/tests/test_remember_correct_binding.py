"""`sibyl correct` on the raw id `sibyl remember` printed reaches the graph row.

The default remember writes the raw memory, then a graph row naming it. The
entity route strips that name with every other server-owned key, so unless it
proves the claim and re-attaches it, the row reaches the graph naming no
capture and a correction on the printed id retires nothing that ranks.

These run the CLI's own capture orchestration against the real route, the
real graph writer and the real correction over an embedded store.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from sibyl.persistence.surreal import content as api_content
from sibyl_core.auth import AuthOrganization
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import get_raw_memory
from tests.harness.auth import stub_auth_context
from tests.harness.remember_correct import (
    CaptureStack,
    cli_remember,
    post_memory_raw,
    wire_capture_stack,
)

TEAMS = {"team-a", "team-b"}


@pytest_asyncio.fixture
async def stack(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CaptureStack]:
    await api_content.close_shared_surreal_content_client()
    organization = AuthOrganization(id=uuid4(), name="Sibyl", slug="sibyl")
    content = SurrealContentClient(url="memory://")
    graph = SurrealGraphClient(group_id=str(organization.id), url="memory://")
    try:
        await bootstrap_content_schema(content, reset=True)
        await prepare_graph_schema(graph)
        yield wire_capture_stack(
            monkeypatch,
            organization=organization,
            content=content,
            graph=graph,
            teams=TEAMS,
        )
    finally:
        await api_content.close_shared_surreal_content_client()
        await content.close()
        await graph.close()


def _member(stack: CaptureStack):
    return stub_auth_context(user_id=uuid4(), organization_id=stack.organization.id)


async def _correct(stack: CaptureStack, ctx, raw_id: str, *, teams: set[str] | None = None):
    """What the correction route hands the core, grants included."""
    return await apply_memory_correction(
        organization_id=stack.group_id,
        source_id=raw_id,
        principal_id=ctx.user_id,
        action="stale",
        reason="no longer true",
        accessible_projects=set(),
        writable_projects=set(),
        maintained_projects=set(),
        accessible_teams=set(teams or ()),
    )


async def _servable(stack: CaptureStack, entity_id: str) -> bool:
    available = await available_graph_entities(
        stack.group_id, [entity_id], runtime=stack.runtime, include_embeddings=False
    )
    return entity_id in available


@pytest.mark.asyncio
async def test_correcting_the_printed_raw_id_retires_the_remembered_row(stack) -> None:
    ctx = _member(stack)
    payload = await cli_remember(
        stack, ctx, title="Deploy target", content="We deploy the API to fly.io."
    )
    raw_id, entity_id = payload["raw_memory_id"], payload["id"]

    row = await stack.runtime.entity_manager.get(entity_id)
    assert row.metadata["raw_memory_id"] == raw_id
    assert row.metadata["raw_source_id"] == "cli:manual"
    assert row.metadata["source_bindings"] == {raw_id: 1}
    raw = await api_content.get_raw_capture(
        None, organization_id=stack.organization.id, capture_id=raw_id
    )
    sidecar = await api_content.get_raw_capture(
        None,
        organization_id=stack.organization.id,
        capture_id=raw.metadata["projected_capture_id"],
    )
    assert sidecar.entity_id == entity_id
    assert await _servable(stack, entity_id)

    result = await _correct(stack, ctx, raw_id)

    assert result.applied
    assert entity_id in result.affected_entity_ids
    row = await stack.runtime.entity_manager.get(entity_id)
    assert row.metadata["correction_blockers"][raw_id]["blocking"] is True
    assert not await _servable(stack, entity_id)


@pytest.mark.asyncio
async def test_a_forged_raw_id_cannot_bind_another_users_capture(stack) -> None:
    victim, attacker = _member(stack), _member(stack)
    # The victim's raw memory has no graph row yet, the window a racing
    # attacker would aim for, and it is shared, so the attacker can even see it.
    victim_raw = await post_memory_raw(
        stack,
        victim,
        _capture_request("Team plan", "Ship on Friday.", memory_scope="team", scope_key="team-a"),
    )

    planted = await cli_remember(
        stack,
        attacker,
        title="Attacker note",
        content="Anything at all.",
        memory_scope="team",
        scope_key="team-a",
        tamper=lambda body: {**body, "raw_memory_id": victim_raw["id"]},
    )

    row = await stack.runtime.entity_manager.get(planted["id"])
    assert "raw_memory_id" not in row.metadata
    assert "source_bindings" not in row.metadata
    stored = await get_raw_memory(organization_id=stack.group_id, memory_id=victim_raw["id"])
    assert "projected_capture_id" not in stored.metadata

    result = await _correct(stack, victim, victim_raw["id"], teams={"team-a"})
    assert result.applied
    assert planted["id"] not in result.affected_entity_ids
    assert (
        "correction_blockers"
        not in (await stack.runtime.entity_manager.get(planted["id"])).metadata
    )
    assert await _servable(stack, planted["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_scope", "raw_key", "graph_scope", "graph_key"),
    [
        ("private", None, "team", "team-a"),
        ("team", "team-a", "team", "team-b"),
        ("team", "team-a", "private", None),
    ],
)
async def test_a_capture_in_another_audience_does_not_bind(
    stack, raw_scope, raw_key, graph_scope, graph_key
) -> None:
    ctx = _member(stack)
    own_raw = await post_memory_raw(
        stack,
        ctx,
        _capture_request(
            "Scoped", "Visible to one audience.", memory_scope=raw_scope, scope_key=raw_key
        ),
    )

    payload = await cli_remember(
        stack,
        ctx,
        title="Elsewhere",
        content="Written to a different audience.",
        memory_scope=graph_scope,
        scope_key=graph_key,
        tamper=lambda body: {**body, "raw_memory_id": own_raw["id"]},
    )

    row = await stack.runtime.entity_manager.get(payload["id"])
    assert row.metadata["memory_scope"] == graph_scope
    assert "raw_memory_id" not in row.metadata
    stored = await get_raw_memory(organization_id=stack.group_id, memory_id=own_raw["id"])
    assert "projected_capture_id" not in stored.metadata


@pytest.mark.asyncio
async def test_an_already_projected_capture_cannot_take_a_second_row(stack) -> None:
    ctx = _member(stack)
    first = await cli_remember(stack, ctx, title="Original", content="The first projection.")

    second = await cli_remember(
        stack,
        ctx,
        title="Second claimant",
        content="Tries to stand behind the same capture.",
        tamper=lambda body: {**body, "raw_memory_id": first["raw_memory_id"]},
    )

    assert (await stack.runtime.entity_manager.get(first["id"])).metadata["raw_memory_id"] == first[
        "raw_memory_id"
    ]
    assert "raw_memory_id" not in (await stack.runtime.entity_manager.get(second["id"])).metadata


@pytest.mark.asyncio
async def test_a_team_correction_stamps_the_team_row_and_lists_it(stack) -> None:
    ctx = _member(stack)
    payload = await cli_remember(
        stack,
        ctx,
        title="Team incident rule",
        content="Page the on-call before rolling back.",
        memory_scope="team",
        scope_key="team-a",
    )
    raw_id, entity_id = payload["raw_memory_id"], payload["id"]
    assert (await stack.runtime.entity_manager.get(entity_id)).metadata["raw_memory_id"] == raw_id
    assert await _servable(stack, entity_id)

    result = await _correct(stack, ctx, raw_id, teams={"team-a"})

    assert result.applied
    # The receipt names the row and whatever the graph projected from it.
    assert entity_id in result.affected_entity_ids
    assert result.refused_entity_ids == []
    row = await stack.runtime.entity_manager.get(entity_id)
    assert row.metadata["correction_blockers"][raw_id]["blocking"] is True
    assert not await _servable(stack, entity_id)


def _capture_request(
    title: str, content: str, *, memory_scope: str, scope_key: str | None
) -> MemoryCaptureRequest:
    """A `sibyl remember --raw` body, a raw memory with no graph row behind it yet."""
    return MemoryCaptureRequest(
        title=title,
        content=content,
        metadata={"capture_mode": "remember", "capture_surface": "cli"},
        memory_scope=memory_scope,
        scope_key=scope_key,
        capture_surface="cli",
    )
