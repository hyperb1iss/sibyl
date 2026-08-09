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


# --- typed-overfetch arm (knn_type_overfetch) --------------------------------
#
# A selective predicate beside the HNSW bracket forces the walk 10-15x deeper
# regardless of syntax (probed live: group-only 0.48s vs any typed predicate
# 3.2-5.6s at 95K rows), so the arm walks an untyped pool `overfetch` times
# the candidate budget and filters types outside the bracket. A full head is
# exactly the typed KNN head; a shortfall falls back to the classic form.

from sibyl_core.backends.surreal.knn import (  # noqa: E402
    KNN_TYPE_OVERFETCH_CAP,
    knn_overfetch_pool,
)
from sibyl_core.models.entities import EntityType  # noqa: E402
from sibyl_core.retrieval.search import (  # noqa: E402
    RetrievalPlan,
    SearchFilter,
    _node_vector_candidates,
)


def test_overfetch_pool_scales_and_caps() -> None:
    assert knn_overfetch_pool(48, 10) == 480
    assert knn_overfetch_pool(200, 32) == KNN_TYPE_OVERFETCH_CAP
    assert knn_overfetch_pool(48, 1) == 48


class _ScriptedClient:
    """Captures composed queries; serves scripted rows per query label."""

    def __init__(self, group_id: str, rows_by_label: dict[str, list[dict[str, object]]]) -> None:
        self.group_id = group_id
        self.rows_by_label = rows_by_label
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, params))
        label = str(params.get("_query_label") or "")
        return list(self.rows_by_label.get(label, []))


def _entity_row(uuid: str, entity_type: str = "topic", score: float = 0.9) -> dict[str, object]:
    return {
        "record_id": f"entity:{uuid}",
        "uuid": uuid,
        "name": uuid,
        "entity_type": entity_type,
        "summary": uuid,
        "group_id": "org-overfetch",
        "attributes": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "score": score,
    }


def _overfetch_provider(namespace: str) -> DeterministicEmbeddingProvider:
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
async def test_entity_vector_search_off_arm_is_the_classic_typed_query() -> None:
    client = _ScriptedClient("org-overfetch", {})
    manager = EntityManager(
        client,
        group_id=client.group_id,
        embedding_provider=_overfetch_provider("overfetch-off"),
    )
    await manager._vector_search(
        query="off arm",
        entity_types=[EntityType.TOPIC],
        limit=10,
    )
    vector_calls = [
        (q, p) for q, p in client.calls if "name_embedding <|" in q
    ]
    assert len(vector_calls) == 1
    query, params = vector_calls[0]
    assert params.get("_query_label") == "entity.search.vector"
    assert "entity_type IN $entity_types" in query
    assert "<|40, 40|>" in query


@pytest.mark.asyncio
async def test_entity_vector_search_overfetch_walks_untyped_pool_and_filters_outside() -> None:
    # Full yield: the overfetch head fills the candidate budget, so no
    # fallback query runs.
    full_head = [_entity_row(f"hit_{i:03d}") for i in range(40)]
    client = _ScriptedClient(
        "org-overfetch", {"entity.search.vector.overfetch": full_head}
    )
    manager = EntityManager(
        client,
        group_id=client.group_id,
        embedding_provider=_overfetch_provider("overfetch-on"),
    )
    results = await manager._vector_search(
        query="on arm",
        entity_types=[EntityType.TOPIC],
        limit=10,
        knn_type_overfetch=10,
    )
    vector_calls = [(q, p) for q, p in client.calls if "name_embedding <|" in q]
    assert [p.get("_query_label") for _, p in vector_calls] == [
        "entity.search.vector.overfetch"
    ]
    query, _params = vector_calls[0]
    # Inner bracket walks the untyped pool (40 * 10); the type filter sits
    # outside the bracket, i.e. after it in the composed text.
    assert "<|400, 400|>" in query
    assert "entity_type IN $entity_types" in query
    assert query.index("entity_type IN $entity_types") > query.index("name_embedding <|")
    assert len(results) == 40


@pytest.mark.asyncio
async def test_entity_vector_search_overfetch_shortfall_falls_back_to_classic() -> None:
    short_head = [_entity_row(f"few_{i}") for i in range(3)]
    classic_head = [_entity_row(f"classic_{i}") for i in range(12)]
    client = _ScriptedClient(
        "org-overfetch",
        {
            "entity.search.vector.overfetch": short_head,
            "entity.search.vector": classic_head,
        },
    )
    manager = EntityManager(
        client,
        group_id=client.group_id,
        embedding_provider=_overfetch_provider("overfetch-fallback"),
    )
    results = await manager._vector_search(
        query="fallback",
        entity_types=[EntityType.TOPIC],
        limit=10,
        knn_type_overfetch=10,
    )
    labels = [
        params.get("_query_label")
        for query, params in client.calls
        if "name_embedding <|" in query
    ]
    assert labels == ["entity.search.vector.overfetch", "entity.search.vector"]
    assert len(results) == 12
    assert all(entity.id.startswith("classic_") for entity, _ in results)


@pytest.mark.asyncio
async def test_node_vector_lane_overfetch_and_fallback_shapes() -> None:
    plan = RetrievalPlan(
        query="lane",
        organization_id="org-overfetch",
        facets=(),
        facet_types={},
        scopes=(),
        denied_scopes=(),
    )

    class _LaneClient:
        def __init__(self, first_rows: int) -> None:
            self.first_rows = first_rows
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
            self.calls.append((query, params))
            count = self.first_rows if len(self.calls) == 1 else 8
            return [_entity_row(f"lane_{len(self.calls)}_{i}") for i in range(count)]

    meta = EmbeddingMetadata(
        provider="deterministic",
        model="unit-test",
        dimensions=EMBEDDING_DIM,
        cache_namespace="lane-overfetch",
        tokenizer_estimate_method="utf8-byte-length",
    )
    # Full yield: one query, type filter outside the bracket.
    client = _LaneClient(first_rows=8)
    await _node_vector_candidates(
        client=client,
        plan=plan,
        search_filter=SearchFilter(node_types=("topic",), knn_type_overfetch=10),
        query_embedding=[0.0] * EMBEDDING_DIM,
        embedding_metadata=meta,
        limit=8,
    )
    assert len(client.calls) == 1
    query, _ = client.calls[0]
    assert "<|80, 80|>" in query
    assert query.index("entity_type IN $node_types") > query.index("name_embedding <|")
    # Shortfall: fallback second query in the classic shape.
    client = _LaneClient(first_rows=2)
    await _node_vector_candidates(
        client=client,
        plan=plan,
        search_filter=SearchFilter(node_types=("topic",), knn_type_overfetch=10),
        query_embedding=[0.0] * EMBEDDING_DIM,
        embedding_metadata=meta,
        limit=8,
    )
    assert len(client.calls) == 2
    fallback_query, _ = client.calls[1]
    assert fallback_query.index("entity_type IN $node_types") < fallback_query.index(
        "name_embedding <|"
    )
    assert "<|8, 40|>" in fallback_query


@pytest.mark.asyncio
async def test_overfetch_head_matches_classic_head_on_the_embedded_engine() -> None:
    client = SurrealGraphClient(group_id="org-overfetch-sem", url="memory://")
    provider = _overfetch_provider("overfetch-sem")
    try:
        await prepare_graph_schema(client)
        rng = random.Random(11)
        rows = [
            {
                "uuid": f"of_{index:03d}",
                "group_id": client.group_id,
                "name": f"Overfetch member {index}",
                "entity_type": ("topic", "task")[index % 2],
                "name_embedding": [rng.random() for _ in range(EMBEDDING_DIM)],
                "created_at": datetime.now(UTC),
            }
            for index in range(120)
        ]
        await client.execute_query("INSERT INTO entity $rows;", rows=rows)
        manager = EntityManager(
            client,
            group_id=client.group_id,
            embedding_provider=provider,
        )
        classic = await manager._vector_search(
            query="overfetch parity",
            entity_types=[EntityType.TOPIC],
            limit=10,
        )
        armed = await manager._vector_search(
            query="overfetch parity",
            entity_types=[EntityType.TOPIC],
            limit=10,
            knn_type_overfetch=10,
        )
    finally:
        await client.close()
    assert sorted(e.id for e, _ in classic) == sorted(e.id for e, _ in armed)
