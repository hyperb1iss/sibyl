"""Retrieval reads a graph row's scope the way the entity model does.

A graph row carries its scope twice: stamped into the attribute bag, and
promoted to the indexed ``memory_scope`` column. The entity model folds the
column in when the bag lost its stamp. Retrieval read the bag alone and its
lanes never selected the column, so a row whose scope survived only in the
column reached the read rule unscoped and was admitted to every member of its
project, or to the whole organization when it had none.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from sibyl_core.models.context import ContextFacet
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval import _search_candidates as candidate_module
from sibyl_core.retrieval import _search_sources as source_module
from sibyl_core.retrieval._search_plan import (
    RetrievalPlan,
    RetrievalSignal,
    SearchFilter,
    build_context_retrieval_plan,
)
from sibyl_core.services.graph import SurrealGraphClient, normalize_records, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_records import entity_from_surreal_row

_ORG = "org-scope-column"


def _plan(principal_id: str, project: str | None, org: str = _ORG) -> RetrievalPlan:
    return build_context_retrieval_plan(
        query="salary",
        organization_id=org,
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["note"]},
        principal_id=principal_id,
        project=project,
        accessible_projects={project} if project else set(),
    )


def _admitted(row: Mapping[str, object], plan: RetrievalPlan) -> bool:
    candidate = candidate_module._candidate_from_node_record(
        row, signal=RetrievalSignal.NODE_FULLTEXT, score=1.0
    )
    return candidate_module._candidate_allowed(
        candidate, plan=plan, requested_types=set(), facet=None
    )


@pytest.mark.parametrize("project", ["project-a", None])
def test_a_column_only_private_row_reaches_its_owner_alone(project: str | None) -> None:
    row = {
        "uuid": "note-column-private",
        "name": "Salary negotiation notes",
        "entity_type": "note",
        "group_id": _ORG,
        "project_id": project,
        "memory_scope": "private",
        "attributes": {"principal_id": "alice", "project_id": project},
    }

    assert not _admitted(row, _plan("bob", project))
    assert _admitted(row, _plan("alice", project))


@pytest.mark.parametrize(
    ("attribute_scope", "column_scope"),
    [
        ("private", None),
        (None, "private"),
        ("private", "private"),
        ("private", "project"),
        ("project", "private"),
        (None, None),
    ],
)
def test_retrieval_and_the_entity_model_read_one_scope(
    attribute_scope: str | None, column_scope: str | None
) -> None:
    attributes: dict[str, object] = {"principal_id": "alice"}
    if attribute_scope is not None:
        attributes["memory_scope"] = attribute_scope
    row = {
        "id": "entity:note-scope",
        "uuid": "note-scope",
        "name": "Scoped note",
        "entity_type": "note",
        "group_id": _ORG,
        "memory_scope": column_scope,
        "attributes": attributes,
    }

    candidate = candidate_module._candidate_from_node_record(
        row, signal=RetrievalSignal.NODE_FULLTEXT, score=1.0
    )

    assert candidate.metadata.get("memory_scope") == entity_from_surreal_row(row).metadata.get(
        "memory_scope"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["project-a", None])
async def test_a_stored_row_that_lost_its_attribute_stamp_stays_private(
    project: str | None,
) -> None:
    # One namespace per case: the embedded engine outlives a closed client.
    org = f"{_ORG}-{project or 'projectless'}"
    client = SurrealGraphClient(group_id=org, url="memory://")
    try:
        await prepare_graph_schema(client)
        await EntityManager(client, group_id=org).create_direct(
            Entity(
                id="note-private",
                entity_type=EntityType.NOTE,
                name="Salary negotiation notes",
                description="salary numbers I keep to myself",
                content="salary numbers I keep to myself",
                organization_id=org,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                metadata={
                    "memory_scope": "private",
                    "principal_id": "alice",
                    **({"project_id": project} if project else {}),
                },
            )
        )
        await client.execute_query(
            "UPDATE entity SET attributes.memory_scope = NONE WHERE uuid = 'note-private';"
        )
        stored = normalize_records(
            await client.execute_query(
                "SELECT memory_scope, attributes FROM entity WHERE uuid = 'note-private';"
            )
        )
        assert stored[0]["memory_scope"] == "private"
        assert "memory_scope" not in stored[0]["attributes"]

        search_filter = SearchFilter(project_ids=(project,) if project else ())
        for reader, admitted in (("bob", False), ("alice", True)):
            plan = _plan(reader, project, org)
            lane = await source_module._node_fulltext_candidates(
                client=client, plan=plan, search_filter=search_filter, limit=5
            )
            assert [candidate.id for candidate in lane] == ["note-private"]
            assert (
                candidate_module._candidate_allowed(
                    lane[0], plan=plan, requested_types=set(), facet=None
                )
                is admitted
            ), reader

        # Graph expansion hydrates whole rows rather than the lane projection.
        full_row = normalize_records(
            await client.execute_query("SELECT * FROM entity WHERE uuid = 'note-private';")
        )[0]
        assert not _admitted(full_row, _plan("bob", project, org))
        assert _admitted(full_row, _plan("alice", project, org))
    finally:
        await client.close()
