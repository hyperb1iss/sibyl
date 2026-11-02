"""Graph node lanes read the project-less rows the read rule admits.

Every node lane narrows by project in SurrealQL before the read rule runs in
Python. The narrowing named only the reader's projects, so a reader holding
any project never fetched a project-less row: not their own project-less
private notes, not their team's notes, and not the legacy unscoped rows the
rule admits org-wide. A reader holding no project got no narrowing at all and
saw them, so granting an API key one project hid rows the narrower key could
read.

A lane fetches a project-less row exactly when the rule could admit it, and
never fetches a row it denies, such as another member's private note.

With an explicit project the read follows that project: its rows, plus the
reader's project-less rows. A row addressed to another project stays out,
even the reader's own private note there, and so does that project's own row.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.embeddings.providers import EmbeddingMetadata
from sibyl_core.memory_pipeline.retrieval_keys import coerce_retrieval_keys
from sibyl_core.models.context import ContextFacet
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval import _search_candidates as candidate_module
from sibyl_core.retrieval import _search_expansion as expansion_module
from sibyl_core.retrieval import _search_sources as source_module
from sibyl_core.retrieval._search_plan import (
    RetrievalPlan,
    RetrievalSignal,
    _search_filter_for_plan,
    build_context_retrieval_plan,
)
from sibyl_core.retrieval.candidates import RetrievalCandidate
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.surreal_content import MemoryScope

READER = "alice"
PROJECT_P = "proj-p"
PROJECT_Q = "proj-q"
PROJECT_R = "proj-r"
TEAM = "team-a"
OTHER_TEAM = "team-b"
RETRIEVAL_KEY = "SAL-7"

_VECTOR = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
_EMBEDDING = EmbeddingMetadata(
    provider="deterministic",
    model="unit-test",
    dimensions=EMBEDDING_DIM,
    cache_namespace="projectless-rows",
    tokenizer_estimate_method="utf8-byte-length",
)

# uuid -> (entity type, project_id, scope metadata)
_ROWS: dict[str, tuple[EntityType, str | None, dict[str, str]]] = {
    "own-projectless-private": (
        EntityType.NOTE,
        None,
        {"memory_scope": "private", "principal_id": READER},
    ),
    "other-projectless-private": (
        EntityType.NOTE,
        None,
        {"memory_scope": "private", "principal_id": "bob"},
    ),
    "legacy-unscoped": (EntityType.NOTE, None, {}),
    "own-private-in-p": (
        EntityType.NOTE,
        PROJECT_P,
        {"memory_scope": "private", "principal_id": READER},
    ),
    "own-private-in-q": (
        EntityType.NOTE,
        PROJECT_Q,
        {"memory_scope": "private", "principal_id": READER},
    ),
    "shared-in-p": (
        EntityType.NOTE,
        PROJECT_P,
        {"memory_scope": "project", "scope_key": PROJECT_P, "principal_id": "bob"},
    ),
    "shared-in-r": (
        EntityType.NOTE,
        PROJECT_R,
        {"memory_scope": "project", "scope_key": PROJECT_R, "principal_id": "carol"},
    ),
    "team-note": (
        EntityType.NOTE,
        None,
        {"memory_scope": "team", "scope_key": TEAM, "principal_id": "bob"},
    ),
    "other-team-note": (
        EntityType.NOTE,
        None,
        {"memory_scope": "team", "scope_key": OTHER_TEAM, "principal_id": "carol"},
    ),
    PROJECT_P: (EntityType.PROJECT, None, {}),
    PROJECT_Q: (EntityType.PROJECT, None, {}),
    PROJECT_R: (EntityType.PROJECT, None, {}),
}

# Alice is a member of P and Q and of one team; R and the other team are not hers.
READABLE_ACROSS_PROJECTS = {
    "own-projectless-private",
    "legacy-unscoped",
    "own-private-in-p",
    "own-private-in-q",
    "shared-in-p",
    "team-note",
    PROJECT_P,
    PROJECT_Q,
}
# An explicit project keeps that project and every project-less row she may read.
READABLE_IN_P = {
    "own-projectless-private",
    "legacy-unscoped",
    "own-private-in-p",
    "shared-in-p",
    "team-note",
    PROJECT_P,
}


@pytest.fixture(params=["embedded", "live"])
async def graph(request: pytest.FixtureRequest) -> AsyncIterator[SurrealGraphClient]:
    """One namespace holding every shape of row, on both engines.

    The live variant runs against a SurrealDB server when
    SIBYL_LIVE_SURREAL_TESTS=1 and SIBYL_SURREAL_URL name one.
    """
    group_id = f"projectless-{uuid4().hex[:10]}"
    if request.param == "embedded":
        client = SurrealGraphClient(group_id=group_id, url="memory://")
    else:
        if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
            pytest.skip("live SurrealDB tests are disabled")
        url = os.environ.get("SIBYL_SURREAL_URL", "")
        if not url or is_embedded_surreal_url(url):
            pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
        client = SurrealGraphClient(
            group_id=group_id,
            url=url,
            username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            namespace_prefix="verify_",
            database="graph",
            pool_size=2,
        )
    try:
        await prepare_graph_schema(client)
        entities = EntityManager(client, group_id=group_id)
        for uuid, (entity_type, project_id, scope) in _ROWS.items():
            await entities.create_direct(
                Entity(
                    id=uuid,
                    entity_type=entity_type,
                    name=f"Salary review {uuid}",
                    description="salary bands for the review",
                    content="salary bands for the review",
                    organization_id=group_id,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    embedding=_VECTOR,
                    metadata={
                        **scope,
                        **({"project_id": project_id} if project_id else {}),
                        "retrieval_keys": [RETRIEVAL_KEY],
                        "embedding_metadata": _EMBEDDING.to_dict(),
                    },
                )
            )
        yield client
    finally:
        if request.param == "live":
            await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        await client.close()


def _plan(
    client: SurrealGraphClient,
    *,
    project: str | None = None,
    accessible_projects: Iterable[str] | None = (PROJECT_P, PROJECT_Q),
    allowed_memory_scope_keys: Iterable[str] | None = None,
) -> RetrievalPlan:
    return build_context_retrieval_plan(
        query="salary",
        organization_id=client.group_id,
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["note"]},
        principal_id=READER,
        project=project,
        accessible_projects=accessible_projects,
        accessible_teams={TEAM},
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )


async def _lane(client: SurrealGraphClient, plan: RetrievalPlan, lane: str) -> list[Any]:
    search_filter = _search_filter_for_plan(plan)
    if lane == "fulltext":
        return await source_module._node_fulltext_candidates(
            client=client, plan=plan, search_filter=search_filter, limit=50
        )
    if lane == "exact_key":
        probes = coerce_retrieval_keys([RETRIEVAL_KEY])
        assert probes is not None
        return await source_module._exact_key_candidates(
            client=client,
            plan=plan,
            search_filter=search_filter,
            limit=50,
            probe_tokens=probes[1],
        )
    if lane == "vector":
        return await source_module._node_vector_candidates(
            client=client,
            plan=plan,
            search_filter=search_filter,
            query_embedding=_VECTOR,
            embedding_metadata=_EMBEDDING,
            limit=50,
        )
    assert lane == "expansion"
    rows = await expansion_module._hydrate_entity_records(
        client=client,
        uuids=list(_ROWS),
        search_filter=search_filter,
        group_id=client.group_id,
        limit=50,
    )
    return [
        candidate_module._candidate_from_node_record(
            row, signal=RetrievalSignal.GRAPH_EXPANSION, score=1.0
        )
        for row in rows
    ]


def _admitted(candidates: Iterable[RetrievalCandidate], plan: RetrievalPlan) -> set[str]:
    return {
        candidate.id
        for candidate in candidates
        if candidate_module._candidate_allowed(
            candidate, plan=plan, requested_types=set(), facet=None
        )
    }


_LANES = ["fulltext", "exact_key", "vector", "expansion"]


def _mark_embedded_vector_gap(
    request: pytest.FixtureRequest, graph: SurrealGraphClient, lane: str
) -> None:
    # The embedded engine evaluates an OR inside an HNSW bracket as false, so
    # any project-narrowed vector lane comes back empty there, before this
    # change and after it. The server engine is what this lane is pinned on.
    if lane == "vector" and graph.is_embedded:
        request.applymarker(
            pytest.mark.xfail(
                strict=True, reason="embedded engine empties an OR inside an HNSW bracket"
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", _LANES)
@pytest.mark.parametrize(
    ("project", "readable"),
    [(None, READABLE_ACROSS_PROJECTS), (PROJECT_P, READABLE_IN_P)],
    ids=["all-projects", "explicit-project"],
)
async def test_a_lane_fetches_exactly_the_rows_the_reader_may_read(
    request: pytest.FixtureRequest,
    graph: SurrealGraphClient,
    lane: str,
    project: str | None,
    readable: set[str],
) -> None:
    _mark_embedded_vector_gap(request, graph, lane)
    plan = _plan(graph, project=project)

    fetched = await _lane(graph, plan, lane)

    # Fetched and admitted agree: nothing the rule admits is narrowed away, and
    # nothing it denies (bob's private note, project R, the other team) is
    # pulled into the pool where it could take a lane slot.
    assert {candidate.id for candidate in fetched} == readable
    assert _admitted(fetched, plan) == readable


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", _LANES)
async def test_granting_a_key_a_project_keeps_its_project_less_rows(
    request: pytest.FixtureRequest, graph: SurrealGraphClient, lane: str
) -> None:
    _mark_embedded_vector_gap(request, graph, lane)
    private_only = {memory_scope_policy_key(MemoryScope.PRIVATE, READER)}
    with_project = private_only | {memory_scope_policy_key(MemoryScope.PROJECT, PROJECT_P)}

    narrow = _plan(graph, allowed_memory_scope_keys=private_only)
    wide = _plan(graph, allowed_memory_scope_keys=with_project)

    projectless = {"own-projectless-private", "legacy-unscoped"}
    assert _admitted(await _lane(graph, narrow, lane), narrow) == projectless
    assert _admitted(await _lane(graph, wide, lane), wide) == projectless | {
        "own-private-in-p",
        "shared-in-p",
        PROJECT_P,
    }


@pytest.mark.parametrize("accessible_projects", [(), None], ids=["no-projects", "unresolved"])
def test_a_project_row_is_read_through_its_own_project(
    accessible_projects: tuple[str, ...] | None,
) -> None:
    # A project row carries no project_id, so the rule addresses it by its own
    # id. Read as project-less, every project's row reached a reader holding no
    # project at all.
    candidate = candidate_module._candidate_from_node_record(
        {
            "uuid": PROJECT_R,
            "name": "Project R",
            "entity_type": "project",
            "group_id": "org",
            "attributes": {"entity_type": "project"},
        },
        signal=RetrievalSignal.NODE_FULLTEXT,
        score=1.0,
    )
    plan = build_context_retrieval_plan(
        query="project",
        organization_id="org",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["project"]},
        principal_id=READER,
        project=None,
        accessible_projects=accessible_projects,
    )
    member_plan = build_context_retrieval_plan(
        query="project",
        organization_id="org",
        facets=[ContextFacet.ACTIVE_WORK],
        facet_types={ContextFacet.ACTIVE_WORK: ["project"]},
        principal_id=READER,
        project=None,
        accessible_projects={PROJECT_R},
    )

    assert not _admitted([candidate], plan)
    assert _admitted([candidate], member_plan) == {PROJECT_R}
