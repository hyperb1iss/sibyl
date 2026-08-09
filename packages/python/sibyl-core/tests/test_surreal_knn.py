"""Effective KNN search effort for Surreal HNSW reads."""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import sibyl_core
from sibyl_core.backends.surreal.knn import knn_search_effort
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.retrieval.dedup import DedupConfig, EntityDeduplicator
from sibyl_core.services.graph import (
    EntityManager,
    SurrealGraphClient,
    normalize_records,
    prepare_graph_schema,
)


def test_effort_rises_to_the_requested_pool_depth() -> None:
    # An HNSW read returns at most `ef` rows, so a pool deeper than the
    # configured effort has to raise it or the read comes back short.
    assert knn_search_effort(100, 40) == 100
    assert knn_search_effort(200, 40) == 200


def test_effort_keeps_the_configured_floor_for_shallow_pools() -> None:
    # The configured effort is a quality floor, so a shallow pool must not
    # lower it.
    assert knn_search_effort(8, 40) == 40
    assert knn_search_effort(32, 88) == 88


def test_effort_stays_positive_for_degenerate_pools() -> None:
    assert knn_search_effort(0, 1) == 1
    assert knn_search_effort(-5, 1) == 1


def test_default_configuration_leaves_deep_pools_short_without_the_floor() -> None:
    # Pins the shipped default the fix has to survive: at ef 40 a 100-row pool
    # is only fully served because `k` raises the effort.
    assert settings.graph_knn_ef == 40
    assert knn_search_effort(100, settings.graph_knn_ef) == 100
    assert knn_search_effort(8, settings.graph_knn_ef) == 40


KNN_CLAUSE_PATTERN = re.compile(r"<\|[^,|]+,\s*([^|]+)\|>")


def knn_clause_offenders(root: Path, allowed_files: set[str]) -> list[tuple[str, str]]:
    """Return (file, effort) for `<|k, ef|>` clauses whose effort skips the helper."""
    offenders: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in allowed_files:
            continue
        # Whole-file scan rather than per-line: a clause wrapped across lines
        # has to be caught too.
        for match in KNN_CLAUSE_PATTERN.finditer(path.read_text(encoding="utf-8")):
            effort = " ".join(match.group(1).split())
            if not effort.startswith("{") or not effort.rstrip("}").endswith("knn_effort"):
                offenders.append((path.name, effort))
    return offenders


def test_every_core_knn_clause_takes_its_effort_from_the_helper() -> None:
    # The defect this module guards is a literal or unfloored effort reaching a
    # `<|k, ef|>` clause. knn.py documents the shape and query_plan_probes.py
    # measures explicit efforts on purpose, so both are exempt.
    root = Path(sibyl_core.__file__).parent
    assert knn_clause_offenders(root, {"knn.py", "query_plan_probes.py"}) == []


async def _seed_entities(client: SurrealGraphClient, count: int) -> None:
    """Insert `count` HNSW-indexed entity rows with distinct embeddings."""
    rng = random.Random(count)
    rows = [
        {
            "uuid": f"knn_pool_{index:04d}",
            "group_id": client.group_id,
            "name": f"Pool member {index}",
            "entity_type": "topic",
            "name_embedding": [rng.random() for _ in range(EMBEDDING_DIM)],
            "created_at": datetime.now(UTC),
        }
        for index in range(count)
    ]
    await client.execute_query("INSERT INTO entity $rows;", rows=rows)


def _knn_shape(query: str) -> str:
    return "<|" + query.split("name_embedding <|")[1].split("|>")[0] + "|>"


@pytest.mark.asyncio
async def test_dedup_lanes_read_the_whole_candidate_pool_on_the_embedded_engine() -> None:
    # The dedup pool is 100 candidates at the shipped batch size, well past the
    # configured effort of 40, so an unfloored effort silently returns 40 rows.
    client = SurrealGraphClient(group_id="org-knn-dedup-pool", url="memory://")
    seen: list[tuple[str, int]] = []
    try:
        await prepare_graph_schema(client)
        await _seed_entities(client, 150)
        manager = EntityManager(client, group_id=client.group_id)
        dedup = EntityDeduplicator(
            client=client,
            entity_manager=manager,
            config=DedupConfig(batch_size=100, same_type_only=True, min_name_overlap=0.0),
        )

        async def counting_query(query: str, **params: object) -> Any:
            rows = await client.execute_query(query, **params)
            seen.append((_knn_shape(query), len(normalize_records(rows))))
            return rows

        rng = random.Random(5)
        seeds = [
            (
                f"knn_seed_{index}",
                f"Seed {index}",
                "topic",
                [rng.random() for _ in range(EMBEDDING_DIM)],
            )
            for index in range(2)
        ]

        await dedup._find_hnsw_candidates_for_seeds(
            seeds[:1],
            group_id=client.group_id,
            entity_types=["topic"],
            threshold=-1.0,
            seen_pairs=set(),
            execute_query=counting_query,
            execute_query_raw=counting_query,
        )
        batch_lane = seen[-1]

        await dedup._find_hnsw_candidates_for_seeds(
            seeds,
            group_id=client.group_id,
            entity_types=["topic"],
            threshold=-1.0,
            seen_pairs=set(),
            execute_query=counting_query,
            execute_query_raw=None,
        )
        per_seed_lanes = seen[-2:]
    finally:
        await client.close()

    assert batch_lane == ("<|100, 100|>", 100)
    assert per_seed_lanes == [("<|100, 100|>", 100), ("<|100, 100|>", 100)]


@pytest.mark.asyncio
async def test_entity_search_vector_lane_reads_the_whole_pool_on_the_embedded_engine() -> None:
    # EntityManager.search overfetches 4x the request, so limit 50 asks for a
    # 200-row pool against a configured effort of 40.
    client = SurrealGraphClient(group_id="org-knn-entity-pool", url="memory://")
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=EMBEDDING_DIM,
            cache_namespace="knn-pool-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    try:
        await prepare_graph_schema(client)
        await _seed_entities(client, 250)
        manager = EntityManager(
            client,
            group_id=client.group_id,
            embedding_provider=provider,
        )

        results = await manager._vector_search(query="pool depth", entity_types=None, limit=50)
    finally:
        await client.close()

    assert len(results) == 200


# --- IN-list fan-out guards (the HNSW planner trap) -------------------------
#
# An `IN $list` predicate beside a `<|k, ef|>` bracket drops the planner off
# the HNSW index into a table scan (measured 9.6s vs 0.5s at 95K rows on the
# live 3.2.0 engine). Typed vector lanes therefore fan out into per-type
# equality queries and merge client-side; these tests pin both the composed
# query shape and the merge semantics.

from sibyl_core.backends.surreal.knn import merge_knn_row_batches  # noqa: E402
from sibyl_core.models.entities import EntityType  # noqa: E402
from sibyl_core.retrieval.search import (  # noqa: E402
    RetrievalPlan,
    SearchFilter,
    _edge_vector_candidates,
    _node_vector_candidates,
)


def test_merge_knn_row_batches_orders_and_trims_like_the_sql() -> None:
    batches = [
        [
            {"uuid": "b", "score": 0.9, "created_at": "2026-01-02"},
            {"uuid": "d", "score": 0.5, "created_at": "2026-01-01"},
        ],
        [
            {"uuid": "a", "score": 0.9, "created_at": "2026-01-03"},
            {"uuid": "c", "score": 0.7, "created_at": "2026-01-01"},
        ],
    ]
    merged = merge_knn_row_batches(batches, limit=3)
    # score DESC first, created_at DESC breaking the 0.9 tie, trim to 3.
    assert [row["uuid"] for row in merged] == ["a", "b", "c"]


class _CapturingClient:
    """Records every composed query; returns no rows."""

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        return []


def _deterministic_provider(namespace: str) -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=EMBEDDING_DIM,
            cache_namespace=namespace,
            tokenizer_estimate_method="utf8-byte-length",
        )
    )


@pytest.mark.asyncio
async def test_typed_entity_vector_search_fans_out_with_equality_predicates() -> None:
    client = _CapturingClient(group_id="org-knn-fanout")
    manager = EntityManager(
        client,
        group_id=client.group_id,
        embedding_provider=_deterministic_provider("knn-fanout-shape"),
    )
    await manager._vector_search(
        query="fan out",
        entity_types=[EntityType.TOPIC, EntityType.TASK, EntityType.NOTE],
        limit=10,
    )
    vector_calls = [
        (query, params)
        for query, params in client.calls
        if params.get("_query_label") == "entity.search.vector"
    ]
    assert len(vector_calls) == 3
    assert sorted(params["entity_type"] for _, params in vector_calls) == [
        "note",
        "task",
        "topic",
    ]
    for query, params in vector_calls:
        assert "entity_type = $entity_type" in query
        assert " IN $" not in query
        assert "entity_types" not in params


@pytest.mark.asyncio
async def test_untyped_entity_vector_search_stays_a_single_bare_query() -> None:
    client = _CapturingClient(group_id="org-knn-bare")
    manager = EntityManager(
        client,
        group_id=client.group_id,
        embedding_provider=_deterministic_provider("knn-bare-shape"),
    )
    await manager._vector_search(query="bare", entity_types=None, limit=10)
    vector_calls = [
        (query, params)
        for query, params in client.calls
        if params.get("_query_label") == "entity.search.vector"
    ]
    assert len(vector_calls) == 1
    query, params = vector_calls[0]
    assert "entity_type" not in params
    assert "entity_type =" not in query
    assert " IN $" not in query


@pytest.mark.asyncio
async def test_typed_entity_vector_search_matches_the_in_form_on_the_embedded_engine() -> None:
    # Semantics guard: the per-type fan-out head must equal the head the old
    # IN-form query returns on the same corpus (the embedded engine serves
    # IN + KNN correctly; only the live planner's speed differs).
    client = SurrealGraphClient(group_id="org-knn-fanout-sem", url="memory://")
    provider = _deterministic_provider("knn-fanout-sem")
    try:
        await prepare_graph_schema(client)
        rng = random.Random(7)
        rows = [
            {
                "uuid": f"sem_{index:03d}",
                "group_id": client.group_id,
                "name": f"Semantics member {index}",
                "entity_type": ("topic", "task", "note")[index % 3],
                "name_embedding": [rng.random() for _ in range(EMBEDDING_DIM)],
                "created_at": datetime.now(UTC),
            }
            for index in range(90)
        ]
        await client.execute_query("INSERT INTO entity $rows;", rows=rows)
        manager = EntityManager(
            client,
            group_id=client.group_id,
            embedding_provider=provider,
        )

        results = await manager._vector_search(
            query="semantics head",
            entity_types=[EntityType.TOPIC, EntityType.TASK],
            limit=10,
        )

        embeddings = await provider.embed_texts(["semantics head"], input_kind="query")
        candidate_limit = 40  # min(max(10 * 4, 32), 200)
        effort = knn_search_effort(candidate_limit, settings.graph_knn_ef)
        in_form = normalize_records(
            await client.execute_query(
                "SELECT * FROM ("
                "SELECT uuid, (1 - vector::distance::knn()) AS score FROM entity "
                "WHERE group_id = $group_id AND entity_type IN $entity_types "
                f"AND name_embedding <|{candidate_limit}, {effort}|> $query_embedding"
                ") ORDER BY score DESC, created_at DESC, uuid DESC LIMIT $limit;",
                group_id=client.group_id,
                entity_types=["topic", "task"],
                query_embedding=list(embeddings[0]),
                limit=candidate_limit,
            )
        )
    finally:
        await client.close()

    fanout_uuids = sorted(entity.id for entity, _ in results)
    in_form_uuids = sorted(str(row["uuid"]) for row in in_form)
    assert fanout_uuids == in_form_uuids
    seeded_types = {row["uuid"]: row["entity_type"] for row in rows}
    assert all(seeded_types[uuid] in {"topic", "task"} for uuid in fanout_uuids)


def _minimal_plan() -> RetrievalPlan:
    return RetrievalPlan(
        query="fan out",
        organization_id="org-knn-lane",
        facets=(),
        facet_types={},
        scopes=(),
        denied_scopes=(),
    )


_LANE_EMBEDDING_METADATA = EmbeddingMetadata(
    provider="deterministic",
    model="unit-test",
    dimensions=EMBEDDING_DIM,
    cache_namespace="knn-lane-shape",
    tokenizer_estimate_method="utf8-byte-length",
)


@pytest.mark.asyncio
async def test_node_vector_lane_fans_out_types_with_equality() -> None:
    client = _CapturingClient(group_id="org-knn-lane")
    await _node_vector_candidates(
        client=client,
        plan=_minimal_plan(),
        search_filter=SearchFilter(node_types=("topic", "task")),
        query_embedding=[0.0] * EMBEDDING_DIM,
        embedding_metadata=_LANE_EMBEDDING_METADATA,
        limit=8,
    )
    assert len(client.calls) == 2
    assert sorted(params["node_type"] for _, params in client.calls) == ["task", "topic"]
    for query, params in client.calls:
        assert "entity_type = $node_type" in query
        assert " IN $" not in query
        assert "node_types" not in params


@pytest.mark.asyncio
async def test_edge_vector_lane_fans_out_types_with_equality() -> None:
    client = _CapturingClient(group_id="org-knn-lane")
    await _edge_vector_candidates(
        client=client,
        plan=_minimal_plan(),
        search_filter=SearchFilter(edge_types=("mentions", "relates")),
        query_embedding=[0.0] * EMBEDDING_DIM,
        embedding_metadata=_LANE_EMBEDDING_METADATA,
        limit=8,
    )
    assert len(client.calls) == 2
    assert sorted(params["edge_type"] for _, params in client.calls) == ["mentions", "relates"]
    for query, params in client.calls:
        assert "name = $edge_type" in query
        assert " IN $" not in query
        assert "edge_types" not in params
