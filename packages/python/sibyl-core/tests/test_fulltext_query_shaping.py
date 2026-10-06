"""Native row and candidate equivalence under the lane projection.

The node lanes project the columns the candidate builder reads and leave the
vector and the body in the database; the body is fetched once for the fused
page. A lane row therefore has to agree with the full row on every projected
column, and the candidate built from it has to agree with the one built from
the full row once its body is hydrated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest

from sibyl_core.backends.surreal.fulltext import build_match_disjunction
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval._search_candidates import _candidate_from_node_record
from sibyl_core.retrieval._search_plan import RetrievalSignal
from sibyl_core.retrieval._search_sources import (
    NODE_CANDIDATE_FIELDS,
    NODE_FULLTEXT_FIELDS,
    _hydrate_candidate_bodies,
    _node_fulltext_field_rows,
)
from sibyl_core.services.graph import SurrealGraphClient, normalize_records, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager


def _projected(row: dict[str, object]) -> dict[str, object]:
    projected = {key: value for key, value in row.items() if key in NODE_CANDIDATE_FIELDS}
    attributes = row.get("attributes")
    if isinstance(attributes, dict):
        projected["attributes"] = {
            key: value for key, value in attributes.items() if key != "content"
        }
    return projected


@pytest.mark.asyncio
@pytest.mark.parametrize("field", NODE_FULLTEXT_FIELDS)
async def test_fulltext_lane_projection_preserves_native_candidates(field: str) -> None:
    client = SurrealGraphClient(group_id=f"org-fulltext-differential-{field}", url="memory://")
    try:
        await prepare_graph_schema(client)
        manager = EntityManager(client, group_id=client.group_id)
        for identity, project in [("a", "project-a"), ("b", "project-a"), ("hidden", "project-b")]:
            await manager.create_direct(
                Entity(
                    id=identity,
                    entity_type=EntityType.TOPIC,
                    name="quartz evidence",
                    description="quartz evidence",
                    content="quartz evidence, the full body a lane never ships",
                    organization_id=client.group_id,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    metadata={
                        "project_id": project,
                        "source_ids": ["raw-a"],
                        "confidence": 0.8,
                        "valid_at": "2026-01-01T00:00:00Z",
                        "future_metadata": {"nested": [1, "kept"]},
                        "embedding": ["nested metadata must remain"],
                    },
                )
            )
        # Ordinary fixtures carry large stored vectors without any provider call.
        await client.execute_query(
            "UPDATE entity SET name_embedding=$vector, "
            "summary='quartz evidence', description='quartz evidence' WHERE group_id=$group_id;",
            vector=[0.125] * 1024,
            group_id=client.group_id,
        )
        match = build_match_disjunction([field], ["quartz"])
        assert match is not None
        params = {"group_id": client.group_id, "project": "project-a", "limit": 2, **match.params}
        expected = normalize_records(
            await client.execute_query(
                f"SELECT *, {match.score_expr} AS score FROM entity "
                "WHERE group_id=$group_id AND project_id=$project "
                f"AND {match.where_clause} "
                "ORDER BY score DESC, created_at DESC, uuid DESC LIMIT $limit;",
                **params,
            )
        )
        actual = await _node_fulltext_field_rows(
            client=client,
            field=field,
            terms=["quartz"],
            organization_id=client.group_id,
            filter_clauses=["project_id=$project"],
            filter_params={"project": "project-a"},
            limit=2,
        )
        assert len(actual) == len(expected) == 2
        assert [row["uuid"] for row in actual] == ["b", "a"]
        # A projected column the row never set comes back as NONE where
        # SELECT * leaves the key out; the builder treats both as absent.
        assert [
            {key: value for key, value in row.items() if value is not None} for row in actual
        ] == [{**_projected(row), "score": row["score"]} for row in expected]
        assert all("name_embedding" not in row and "content" not in row for row in actual)
        lane_candidates = [
            _candidate_from_node_record(
                row, signal=RetrievalSignal.NODE_FULLTEXT, score=float(cast("float", row["score"]))
            )
            for row in actual
        ]
        full_candidates = [
            _candidate_from_node_record(
                row, signal=RetrievalSignal.NODE_FULLTEXT, score=float(cast("float", row["score"]))
            )
            for row in expected
        ]
        # Before hydration the lane candidate carries the builder's fallback
        # body; after one indexed read it is the candidate the full row gives.
        assert [candidate.content for candidate in lane_candidates] == ["quartz evidence"] * 2
        hydrated = await _hydrate_candidate_bodies(
            client=client,
            group_id=client.group_id,
            fused=[
                (candidate, candidate.score, {"sources": [RetrievalSignal.NODE_FULLTEXT.value]})
                for candidate in lane_candidates
            ],
        )
        assert [candidate for candidate, _score, _metadata in hydrated] == full_candidates
        assert len(json.dumps(actual, default=str)) < len(json.dumps(expected, default=str)) / 2
    finally:
        await client.close()
