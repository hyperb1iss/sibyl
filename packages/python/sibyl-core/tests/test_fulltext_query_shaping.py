"""Native row and candidate equivalence when omitting fulltext vectors."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sibyl_core.backends.surreal.fulltext import build_match_disjunction
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval._search_candidates import _candidate_from_node_record
from sibyl_core.retrieval._search_plan import RetrievalSignal
from sibyl_core.retrieval._search_sources import NODE_FULLTEXT_FIELDS, _node_fulltext_field_rows
from sibyl_core.services.graph import SurrealGraphClient, normalize_records, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager


@pytest.mark.asyncio
@pytest.mark.parametrize("field", NODE_FULLTEXT_FIELDS)
async def test_fulltext_vector_omission_preserves_native_candidates(field: str) -> None:
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
                    content="quartz evidence",
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
        assert actual == [
            {key: value for key, value in row.items() if key != "name_embedding"}
            for row in expected
        ]
        for old, new in zip(expected, actual, strict=True):
            assert _candidate_from_node_record(
                new,
                signal=RetrievalSignal.NODE_FULLTEXT,
                score=float(new["score"]),
            ) == _candidate_from_node_record(
                old,
                signal=RetrievalSignal.NODE_FULLTEXT,
                score=float(old["score"]),
            )
        assert len(json.dumps(actual, default=str)) < len(json.dumps(expected, default=str)) / 2
    finally:
        await client.close()
