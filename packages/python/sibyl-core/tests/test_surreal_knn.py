"""Effective KNN search effort for Surreal HNSW reads."""

from __future__ import annotations

import math
import os
import random
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import sibyl_core
from sibyl_core.backends.surreal.knn import knn_search_effort
from sibyl_core.backends.surreal.schema import (
    ANALYZER_DEFINITIONS,
    EDGE_DEFINITIONS,
    EMBEDDING_DIM,
    ENTITY_TYPED_VECTOR_SPACE_INDEX_DEFINITIONS,
    ENTITY_VECTOR_SPACE_INDEX_DEFINITIONS,
    NODE_DEFINITIONS,
    _graph_schema_migrations,
    render_surreal_compatible_sql,
)
from sibyl_core.backends.surreal.schema_version import (
    apply_schema_migrations,
    get_schema_version,
)
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


async def _seed_entities(
    client: SurrealGraphClient, count: int, *, stamp: dict[str, object] | None = None
) -> None:
    """Insert `count` HNSW-indexed entity rows with distinct embeddings.

    Vector lanes score only rows stamped with the query provider's metadata,
    so a lane under test needs ``stamp`` set to that provider's metadata.
    """
    rng = random.Random(count)
    rows = [
        {
            "uuid": f"knn_pool_{index:04d}",
            "group_id": client.group_id,
            "name": f"Pool member {index}",
            "entity_type": "topic",
            "name_embedding": [rng.random() for _ in range(EMBEDDING_DIM)],
            "attributes": {"embedding_metadata": stamp} if stamp is not None else {},
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
        await _seed_entities(client, 250, stamp=provider.metadata.to_dict())
        manager = EntityManager(
            client,
            group_id=client.group_id,
            embedding_provider=provider,
        )

        results = await manager._vector_search(query="pool depth", entity_types=None, limit=50)
    finally:
        await client.close()

    assert len(results) == 200


@pytest.mark.asyncio
async def test_entity_vector_lane_ignores_vectors_from_another_model() -> None:
    client = SurrealGraphClient(group_id="org-knn-entity-space", url="memory://")
    provider = _overfetch_provider("knn-space-test")
    other = _overfetch_provider("knn-space-test", model="another-model").metadata.to_dict()
    try:
        await prepare_graph_schema(client)
        await _seed_entities(client, 30, stamp=other)
        manager = EntityManager(client, group_id=client.group_id, embedding_provider=provider)

        stale = await manager._vector_search(query="pool depth", entity_types=None, limit=5)
        await client.execute_query(
            "UPDATE entity SET attributes.embedding_metadata = $stamp WHERE uuid < 'knn_pool_0010';",
            stamp=provider.metadata.to_dict(),
        )
        current = await manager._vector_search(query="pool depth", entity_types=None, limit=5)
    finally:
        await client.close()

    # A query is never scored against another model's vectors; once the sweep
    # restamps them, exactly those rows come back.
    assert stale == []
    assert {entity.id for entity, _score in current} == {
        f"knn_pool_{index:04d}" for index in range(10)
    }


@pytest.mark.asyncio
async def test_entity_vector_lane_keeps_vectors_whose_stamp_differs_only_in_bookkeeping() -> None:
    client = SurrealGraphClient(group_id="org-knn-entity-bookkeeping", url="memory://")
    provider = _overfetch_provider("knn-space-test")
    # Same provider, model and size; only the cache namespace and the token
    # estimator moved, neither of which changes a vector.
    renamed = _overfetch_provider("renamed-cache", tokenizer="another-estimator")
    try:
        await prepare_graph_schema(client)
        await _seed_entities(client, 12, stamp=renamed.metadata.to_dict())
        manager = EntityManager(client, group_id=client.group_id, embedding_provider=provider)
        found = await manager._vector_search(query="pool depth", entity_types=None, limit=5)
    finally:
        await client.close()

    assert found
    assert {entity.id for entity, _score in found} <= {f"knn_pool_{i:04d}" for i in range(12)}


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
from sibyl_core.retrieval._search_plan import RetrievalPlan, SearchFilter  # noqa: E402
from sibyl_core.retrieval._search_sources import _node_vector_candidates  # noqa: E402


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


def _overfetch_provider(
    namespace: str, *, model: str = "unit-test", tokenizer: str = "utf8-byte-length"
) -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model=model,
            dimensions=EMBEDDING_DIM,
            cache_namespace=namespace,
            tokenizer_estimate_method=tokenizer,
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
    vector_calls = [(q, p) for q, p in client.calls if "name_embedding <|" in q]
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
    client = _ScriptedClient("org-overfetch", {"entity.search.vector.overfetch": full_head})
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
    assert [p.get("_query_label") for _, p in vector_calls] == ["entity.search.vector.overfetch"]
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
            "entity.search.vector.exact": classic_head,
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
        params.get("_query_label") for query, params in client.calls if "name_embedding <|" in query
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


@pytest.mark.asyncio
async def test_write_time_dedup_compares_only_vectors_in_the_seeds_space() -> None:
    from sibyl_core.embeddings.provenance import (
        UNVERIFIED_ORIGIN_ARCHIVE,
        unverified_embedding_metadata,
    )
    from sibyl_core.models.entities import Entity

    client = SurrealGraphClient(group_id="org-knn-dedup-space", url="memory://")
    space = _overfetch_provider("dedup-space").metadata.to_dict()
    vector = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    try:
        await prepare_graph_schema(client)
        await client.execute_query(
            "INSERT INTO entity $rows;",
            rows=[
                {
                    "uuid": uuid,
                    "group_id": client.group_id,
                    "name": "Twin",
                    "entity_type": "topic",
                    "name_embedding": list(vector),
                    "attributes": {"embedding_metadata": stamp} if stamp else {},
                    "created_at": datetime.now(UTC),
                }
                for uuid, stamp in (("stamped-twin", space), ("unstamped-twin", None))
            ],
        )
        manager = EntityManager(client, group_id=client.group_id)
        dedup = EntityDeduplicator(
            client=client,
            entity_manager=manager,
            config=DedupConfig(same_type_only=True, min_name_overlap=0.0),
        )

        def seed(entity_id: str, stamp: dict[str, object] | None) -> Entity:
            return Entity(
                id=entity_id,
                entity_type=EntityType.TOPIC,
                name="Twin",
                embedding=list(vector),
                metadata={"embedding_metadata": stamp} if stamp else {},
            )

        matches = await dedup.resolve_existing_entities(
            [
                seed("new-stamped", space),
                seed("new-unstamped", None),
                seed("new-unverified", unverified_embedding_metadata(UNVERIFIED_ORIGIN_ARCHIVE)),
            ],
            threshold=0.5,
        )
    finally:
        await client.close()

    assert matches["new-stamped"].entity2_id == "stamped-twin"
    assert matches["new-unstamped"].entity2_id == "unstamped-twin"
    assert "new-unverified" not in matches


@pytest.mark.parametrize("admit_unstamped", [False, True])
async def test_entity_vector_search_completes_filtered_shortfall(
    monkeypatch: pytest.MonkeyPatch, admit_unstamped: bool
) -> None:
    from sibyl_core.services.embedding_lane_readiness import LaneReadiness

    async def ready(**_kwargs: object) -> LaneReadiness:
        return LaneReadiness(run=True, reason="test", admit_unstamped=admit_unstamped)

    monkeypatch.setattr("sibyl_core.services.embedding_lane_readiness.vector_lane_readiness", ready)
    complete = [_entity_row(f"complete_{i:03d}") for i in range(32)]
    complete[0]["attributes"] = {"user_metadata": {"meaningful": "retained"}}
    # Ten hits exceed the caller's five, but still fall short of its candidate
    # pool. Completion must fill that pool, not merely the requested output.
    client = _ScriptedClient(
        "org-overfetch",
        {"entity.search.vector": complete[:10], "entity.search.vector.exact": complete},
    )
    provider = _overfetch_provider("filtered-completion")
    manager = EntityManager(client, group_id=client.group_id, embedding_provider=provider)

    found = await manager._vector_search(
        query="pool depth", entity_types=[EntityType.TOPIC], limit=5
    )

    assert [entity.id for entity, _score in found] == [f"complete_{i:03d}" for i in range(32)]
    assert found[0][0].metadata["user_metadata"] == {"meaningful": "retained"}
    completion = [
        (query, params)
        for query, params in client.calls
        if params.get("_query_label") == "entity.search.vector.exact"
    ]
    assert len(completion) == 1
    query, params = completion[0]
    assert params["limit"] == 32
    assert params["embedding_provider"] == provider.metadata.provider
    assert params["embedding_model"] == provider.metadata.model
    assert params["embedding_dimensions"] == EMBEDDING_DIM
    assert "embedding_metadata" not in params
    assert all(
        (f"attributes.embedding_metadata.{field} = NONE" in query) is admit_unstamped
        for field in ("provider", "model", "dimensions")
    )


@pytest.fixture
async def vector_completion_client():
    client = SurrealGraphClient(
        group_id="vector-completion-" + uuid4().hex,
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    )
    try:
        await prepare_graph_schema(client)
        yield client
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {client.namespace};")
        await client.close()


def _cosine_score(vector: list[float], query: list[float]) -> float:
    return sum(a * b for a, b in zip(vector, query, strict=True)) / math.sqrt(
        sum(a * a for a in vector) * sum(b * b for b in query)
    )


@pytest.mark.parametrize("attempt", range(32), ids=lambda n: f"case-{n:02d}")
async def test_entity_vector_completion_preserves_eligible_random_cohort(
    vector_completion_client: SurrealGraphClient,
    attempt: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from surrealdb.errors import SurrealError

    from sibyl_core.services.graph_records import _entity_from_row

    client = vector_completion_client
    provider = _overfetch_provider("mixed-completion")
    stamp = provider.metadata.to_dict()
    other = stamp | {"model": "another-model"}
    rng = random.Random(8000 + attempt)
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    rows = [
        {
            "uuid": f"knn_pool_{i:04d}" if i < 30 else f"stale_model_{i:04d}",
            "group_id": client.group_id,
            "name": f"Pool member {i}",
            "entity_type": "pattern" if i < 2 else "topic",
            "description": f"Description {i}",
            "content": f"Content {i}",
            "summary": f"Summary {i}",
            "name_embedding": [rng.random() for _ in range(EMBEDDING_DIM)],
            "attributes": {
                "embedding_metadata": other,
                "user_metadata": {"case": attempt, "index": i},
                "memory_scope": "personal",
                "principal_id": "proof-owner",
                "source_file": f"source-{i}.txt",
            },
            "created_at": now,
            "updated_at": now,
            "revision": 7,
            "project_id": "proof-project",
            "created_by": "proof-author",
            "modified_by": "proof-editor",
        }
        for i in range(286)
    ]
    extra_stamps = {
        "wrong_provider": stamp | {"provider": "incompatible-provider"},
        "wrong_dimension_stamp": stamp | {"dimensions": EMBEDDING_DIM - 1},
        "other_org": stamp,
        "missing_vector": stamp,
        "deleted_vector": stamp,
    }
    for identity, metadata in extra_stamps.items():
        row = {
            **rows[0],
            "uuid": identity,
            "entity_type": "topic",
            "attributes": {**rows[0]["attributes"], "embedding_metadata": metadata},
        }
        if identity == "other_org":
            row["group_id"] = "another-organization"
        if identity == "missing_vector":
            row["name_embedding"] = None
        rows.append(row)
    # Start with every vector in another space; the extras become current only
    # after the first read, so both absence and completion are exercised.
    await client.execute_query("INSERT INTO entity $rows;", rows=rows[:286])
    execute = client.execute_query
    exact_reads: list[tuple[str, dict[str, object], list[dict[str, Any]]]] = []

    async def observed(query: str, **params: object) -> object:
        result = await execute(query, **params)
        normalized = normalize_records(result)
        if params.get("_query_label") == "entity.search.vector.exact":
            exact_reads.append((query, params, normalized))
        if attempt == 0 and params.get("_query_label") == "entity.search.vector" and normalized:
            # Deterministically replay the real engine's one-row shortfall.
            return normalized[:1]
        return result

    monkeypatch.setattr(client, "execute_query", observed)
    manager = EntityManager(client, group_id=client.group_id, embedding_provider=provider)
    assert await manager._vector_search(query="pool depth", entity_types=None, limit=5) == []
    await client.execute_query(
        "UPDATE entity SET attributes.embedding_metadata=$stamp WHERE uuid < 'knn_pool_0010';",
        stamp=stamp,
    )
    await client.execute_query("INSERT INTO entity $rows;", rows=rows[286:])
    await client.execute_query("DELETE entity WHERE uuid=$uuid;", uuid="deleted_vector")
    if attempt == 0:
        wrong = {**rows[0], "uuid": "invalid_vector_dimension", "name_embedding": [0.2, 0.5]}
        with pytest.raises(SurrealError):
            await client.execute_query("INSERT INTO entity $rows;", rows=[wrong])

    found = await manager._vector_search(query="pool depth", entity_types=None, limit=5)
    expected = {f"knn_pool_{i:04d}" for i in range(10)}
    assert {entity.id for entity, _score in found} == expected
    query, params, exact = exact_reads[-1]
    query_vector = params["query_embedding"]
    oracle = sorted(
        rows[:10],
        key=lambda row: (
            _cosine_score(row["name_embedding"], query_vector),
            row["created_at"],
            row["uuid"],
        ),
        reverse=True,
    )
    assert [entity.id for entity, _score in found] == [row["uuid"] for row in oracle]
    for (entity, score), row, full in zip(found, oracle, exact, strict=True):
        assert math.isclose(score, _cosine_score(row["name_embedding"], query_vector), abs_tol=2e-6)
        assert entity.model_dump(mode="json") == _entity_from_row(full).model_dump(mode="json")
        assert entity.metadata["user_metadata"] == {"case": attempt, "index": rows.index(row)}
        assert entity.content == row["content"]
        assert entity.description == row["description"]
        assert entity.created_at == now
    for overfetch in (0, 4):
        typed = await manager._vector_search(
            query="pool depth",
            entity_types=[EntityType.TOPIC],
            limit=5,
            knn_type_overfetch=overfetch,
        )
        assert {entity.id for entity, _score in typed} == {
            f"knn_pool_{i:04d}" for i in range(2, 10)
        }
    tombstones = normalize_records(
        await client.execute_query(
            "SELECT source_id, deleted FROM source_states WHERE source_id=$uuid;",
            uuid="deleted_vector",
        )
    )
    assert tombstones == [{"source_id": "deleted_vector", "deleted": True}]
    if attempt == 0 and client._url != "memory://":
        plan = await execute(query.rstrip().removesuffix(";") + " EXPLAIN FULL;", **params)

        def indexed_slice(value: object) -> bool:
            if isinstance(value, dict):
                attributes = value.get("attributes", {})
                if (
                    value.get("operator") == "IndexScan"
                    and attributes.get("index") == "idx_entity_vector_space"
                ):
                    return value.get("metrics", {}).get("output_rows") == 11
                return any(indexed_slice(child) for child in value.values())
            return isinstance(value, list) and any(indexed_slice(child) for child in value)

        assert indexed_slice(plan), plan


async def test_entity_vector_space_index_registered_upgrade_retains_sources(
    vector_completion_client: SurrealGraphClient,
) -> None:
    client = vector_completion_client
    migrations = tuple(
        migration
        for migration in _graph_schema_migrations(url=client._url, group_id=client.group_id)
        if migration.version <= 34
    )
    assert await get_schema_version(client.execute_query, name="graph") == 35
    info = await client.execute_query("INFO FOR TABLE entity;")
    assert "idx_entity_vector_space" in info["indexes"]

    # Install the actual historical registry and base declarations, without
    # the new index, in a second owned namespace.
    historical = SurrealGraphClient(
        group_id="vector-upgrade-" + uuid4().hex,
        url=client._url,
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    )
    try:
        for block in (
            ANALYZER_DEFINITIONS,
            NODE_DEFINITIONS.replace(ENTITY_VECTOR_SPACE_INDEX_DEFINITIONS, "").replace(
                ENTITY_TYPED_VECTOR_SPACE_INDEX_DEFINITIONS, ""
            ),
            EDGE_DEFINITIONS,
        ):
            await historical.execute_query(render_surreal_compatible_sql(block, url=client._url))
        old = tuple(migration for migration in migrations if migration.version <= 33)
        await apply_schema_migrations(historical.execute_query, old, name="graph")
        assert await get_schema_version(historical.execute_query, name="graph") == 33
        assert (
            "idx_entity_vector_space"
            not in (await historical.execute_query("INFO FOR TABLE entity;"))["indexes"]
        )
        await _seed_entities(
            historical, 12, stamp=_overfetch_provider("upgrade").metadata.to_dict()
        )
        before = await historical.execute_query("SELECT * FROM entity ORDER BY uuid;")
        states = await historical.execute_query("SELECT * FROM source_states ORDER BY source_id;")
        applied = await apply_schema_migrations(historical.execute_query, migrations, name="graph")
        assert [migration.version for migration in applied] == [34]
        assert await get_schema_version(historical.execute_query, name="graph") == 34
        assert (
            "idx_entity_vector_space"
            in (await historical.execute_query("INFO FOR TABLE entity;"))["indexes"]
        )
        assert await historical.execute_query("SELECT * FROM entity ORDER BY uuid;") == before
        assert (
            await historical.execute_query("SELECT * FROM source_states ORDER BY source_id;")
            == states
        )
        assert (
            await apply_schema_migrations(historical.execute_query, migrations, name="graph") == []
        )
    finally:
        await historical.execute_query(f"REMOVE NAMESPACE {historical.namespace};")
        await historical.close()


async def test_entity_vector_completion_preserves_unstamped_adoption(
    vector_completion_client: SurrealGraphClient,
) -> None:
    from sibyl_core.backends.surreal.schema_embedding_states import embedding_state_key

    client = vector_completion_client
    provider = _overfetch_provider("unstamped-completion")
    await _seed_entities(client, 10)
    await client.execute_query(
        "INSERT INTO entity $rows;",
        rows=[
            {
                "uuid": identity,
                "group_id": client.group_id,
                "name": identity,
                "entity_type": "topic",
                "name_embedding": None if identity == "missing_vector" else [0.1] * EMBEDDING_DIM,
                "attributes": {
                    "embedding_metadata": provider.metadata.to_dict() | {"model": "another-model"}
                }
                if identity == "another_model"
                else {},
            }
            for identity in ("another_model", "missing_vector")
        ],
    )
    manager = EntityManager(client, group_id=client.group_id, embedding_provider=provider)
    assert await manager._vector_search(query="pool depth", entity_types=None, limit=5) == []
    await client.execute_query(
        "UPSERT type::record($key) SET organization_id=$org, plane='graph', "
        "legacy_decision='adopt', legacy_metadata=$stamp;",
        key=embedding_state_key(client.group_id, "graph"),
        org=client.group_id,
        stamp=provider.metadata.to_dict(),
    )
    found = await manager._vector_search(query="pool depth", entity_types=None, limit=5)
    assert {entity.id for entity, _score in found} == {f"knn_pool_{i:04d}" for i in range(10)}


@pytest.mark.parametrize("use_batch", [True, False], ids=["batch", "single"])
async def test_graph34_index_preserves_stamped_and_unstamped_dedup(
    vector_completion_client: SurrealGraphClient,
    use_batch: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.embeddings.provenance import (
        UNVERIFIED_ORIGIN_ARCHIVE,
        unverified_embedding_metadata,
    )
    from sibyl_core.models.entities import Entity

    client = vector_completion_client
    assert await get_schema_version(client.execute_query, name="graph") == 35
    stamp = _overfetch_provider("dedup-space").metadata.to_dict()
    vector = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    await client.execute_query(
        "INSERT INTO entity $rows;",
        rows=[
            {
                "uuid": identity,
                "group_id": client.group_id,
                "name": "Twin",
                "entity_type": "topic",
                "name_embedding": vector,
                "attributes": {"embedding_metadata": metadata} if metadata else {},
                "created_at": datetime(2026, 9, 30, 12, tzinfo=UTC),
            }
            for identity, metadata in (("stamped-twin", stamp), ("unstamped-twin", None))
        ],
    )
    queries: list[tuple[str, str]] = []
    execute = client.execute_query
    raw = client.execute_query_raw

    async def observed(query: str, **params: object) -> object:
        result = await execute(query, **params)
        if str(params.get("_query_label", "")).startswith("dedup.candidates"):
            queries.append((query, str(params["_query_label"])))
        return result

    async def observed_raw(query: str, **params: object) -> object:
        result = await raw(query, **params)
        queries.append((query, str(params.get("_query_label", ""))))
        return result

    monkeypatch.setattr(client, "execute_query", observed)
    monkeypatch.setattr(client, "execute_query_raw", observed_raw if use_batch else None)
    manager = EntityManager(client, group_id=client.group_id)
    dedup = EntityDeduplicator(
        client=client,
        entity_manager=manager,
        config=DedupConfig(same_type_only=True, min_name_overlap=0.0),
    )
    matches = await dedup.resolve_existing_entities(
        [
            Entity(
                id=identity,
                entity_type=EntityType.TOPIC,
                name="Twin",
                embedding=vector,
                metadata={"embedding_metadata": metadata} if metadata else {},
            )
            for identity, metadata in (
                ("new-stamped", stamp),
                ("new-unstamped", None),
                ("new-unverified", unverified_embedding_metadata(UNVERIFIED_ORIGIN_ARCHIVE)),
            )
        ],
        threshold=0.5,
    )
    assert matches["new-stamped"].entity2_id == "stamped-twin"
    assert matches["new-unstamped"].entity2_id == "unstamped-twin"
    assert "new-unverified" not in matches
    assert [label for _query, label in queries] == (
        ["dedup.candidates.batch"] if use_batch else ["dedup.candidates", "dedup.candidates"]
    )
    assert all("FROM entity WITH INDEX idx_entity_embedding" in query for query, _ in queries)


@pytest.mark.parametrize("overfetch", [0, 4], ids=["classic", "overfetch"])
async def test_graph34_index_preserves_context_vector_consumers(
    vector_completion_client: SurrealGraphClient,
    overfetch: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = vector_completion_client
    provider = _overfetch_provider("context-space")
    stamp = provider.metadata.to_dict()
    vector = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    await client.execute_query(
        "INSERT INTO entity $rows;",
        rows=[
            {
                "uuid": f"knn_pool_{i:04d}",
                "group_id": client.group_id,
                "name": f"Pool member {i}",
                "entity_type": "pattern" if i < 2 else "topic",
                "name_embedding": vector,
                "attributes": {"embedding_metadata": stamp},
                "created_at": datetime(2026, 9, 30, 12, tzinfo=UTC),
            }
            for i in range(12)
        ]
        + [
            {
                "uuid": identity,
                "group_id": client.group_id,
                "name": identity,
                "entity_type": "topic",
                "name_embedding": vector,
                "attributes": {"embedding_metadata": metadata} if metadata else {},
                "created_at": datetime(2026, 9, 30, 12, tzinfo=UTC),
            }
            for identity, metadata in (
                ("zz-other-model", stamp | {"model": "incompatible-model"}),
                ("zz-unstamped", None),
            )
        ],
    )
    queries: list[str] = []
    execute = client.execute_query

    async def observed(query: str, **params: object) -> object:
        result = await execute(query, **params)
        queries.append(query)
        return result

    monkeypatch.setattr(client, "execute_query", observed)
    arguments = {
        "client": client,
        "plan": RetrievalPlan(
            query="pool depth",
            organization_id=client.group_id,
            facets=(),
            facet_types={},
            scopes=(),
            denied_scopes=(),
        ),
        "search_filter": SearchFilter(node_types=("topic",), knn_type_overfetch=overfetch),
        "query_embedding": vector,
        "embedding_metadata": provider.metadata,
        "limit": 8,
    }
    found = await _node_vector_candidates(**arguments)
    assert found
    identities = [candidate.id for candidate in found]
    assert set(identities) <= {f"knn_pool_{i:04d}" for i in range(2, 12)}
    assert identities == sorted(identities, reverse=True)
    assert all(candidate.score == pytest.approx(1.0) for candidate in found)
    assert all("FROM entity WITH INDEX idx_entity_embedding" in query for query in queries)
    assert (
        queries[0].index("entity_type IN $node_types") > queries[0].index("name_embedding <|")
    ) == bool(overfetch)

    # Compare the same physical HNSW graph before the added scalar index.
    # Equal-vector ties belong to the approximate pool until its final sort.
    await execute("REMOVE INDEX idx_entity_vector_space ON entity;")
    await execute("REMOVE INDEX idx_entity_typed_vector_space ON entity;")

    async def without_scalar_index(query: str, **params: object) -> object:
        return await execute(query.replace(" WITH INDEX idx_entity_embedding", ""), **params)

    monkeypatch.setattr(client, "execute_query", without_scalar_index)
    try:
        baseline = await _node_vector_candidates(**arguments)
    finally:
        monkeypatch.setattr(client, "execute_query", observed)
        await execute(ENTITY_VECTOR_SPACE_INDEX_DEFINITIONS)
        await execute(ENTITY_TYPED_VECTOR_SPACE_INDEX_DEFINITIONS)
    assert found == baseline


async def test_entity_vector_completion_orders_the_whole_eligible_slice(
    vector_completion_client: SurrealGraphClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = vector_completion_client
    provider = _overfetch_provider("ordered-completion")
    stamp = provider.metadata.to_dict()
    vector = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    rows = [
        {
            "uuid": f"ordered_{i:04d}",
            "group_id": client.group_id,
            "name": f"Member {i}",
            "entity_type": "topic",
            "name_embedding": vector,
            "attributes": {"embedding_metadata": stamp},
            "created_at": datetime(2026, 9, 30, 12, 0, i % 4, tzinfo=UTC),
        }
        for i in range(40)
    ]
    await client.execute_query("INSERT INTO entity $rows;", rows=rows)
    execute = client.execute_query

    async def shortfall(query: str, **params: object) -> object:
        result = await execute(query, **params)
        if params.get("_query_label") == "entity.search.vector":
            return normalize_records(result)[:1]
        return result

    monkeypatch.setattr(client, "execute_query", shortfall)
    manager = EntityManager(client, group_id=client.group_id, embedding_provider=provider)

    async def query_embedding(_texts: object, **_kwargs: object) -> list[list[float]]:
        return [vector]

    monkeypatch.setattr(provider, "embed_texts", query_embedding)
    found = await manager._vector_search(query="pool depth", entity_types=None, limit=5)
    expected = sorted(rows, key=lambda row: (row["created_at"], row["uuid"]), reverse=True)[:32]
    assert [entity.id for entity, _score in found] == [row["uuid"] for row in expected]
    assert all(score == pytest.approx(1.0) for _entity, score in found)


async def test_graph34_vector_index_preserves_nested_endpoint_reads(
    vector_completion_client: SurrealGraphClient,
) -> None:
    from sibyl_core.models.entities import Entity

    client = vector_completion_client
    manager = EntityManager(client, group_id=client.group_id)
    for identity in ("endpoint-source", "endpoint-target"):
        await manager.create_direct(
            Entity(
                id=identity,
                entity_type=EntityType.TOPIC,
                name=identity,
                metadata={"source_file": "ordinary-lookup.txt"},
            )
        )
    edge = {
        "source_id": "endpoint-source",
        "target_id": "endpoint-target",
        "group_id": client.group_id,
    }
    found = await client.execute_query(
        """
        RETURN $edges.map(|$edge|
            object::from_entries(array::concat(object::entries($edge), [
                ['source_match', (SELECT VALUE uuid FROM entity
                    WHERE uuid=$edge.source_id AND group_id=$edge.group_id LIMIT 1)[0]],
                ['target_match', (SELECT VALUE uuid FROM entity
                    WHERE uuid=$edge.target_id AND group_id=$edge.group_id LIMIT 1)[0]]
            ]))
        );
        """,
        edges=[edge],
    )
    assert normalize_records(found) == [
        {**edge, "source_match": "endpoint-source", "target_match": "endpoint-target"}
    ]


@pytest.mark.parametrize("type_selection", ["untyped", "typed", "all-types"])
@pytest.mark.parametrize("global_limit", [False, True], ids=["complete-cohort", "global-limit"])
async def test_adopted_vector_completion_bounds_all_stamp_partitions(
    vector_completion_client: SurrealGraphClient,
    type_selection: str,
    global_limit: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta
    from itertools import product

    from sibyl_core.backends.surreal.schema_embedding_states import embedding_state_key

    client = vector_completion_client
    provider = _overfetch_provider("adopted-indexed-completion")
    stamp = provider.metadata.to_dict()
    types = (
        None
        if type_selection == "untyped"
        else [EntityType.TOPIC]
        if type_selection == "typed"
        else list(EntityType)
    )
    requested = {kind.value for kind in types or ()}
    fields = ("provider", "model", "dimensions")
    query_vector = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    rows = []
    for present in product((False, True), repeat=3):
        count = 10 if global_limit else 7 if all(present) or not any(present) else 1
        for _ in range(count):
            ordinal = len(rows)
            angle = (ordinal // 4 + 1) / 50
            rows.append(
                {
                    "uuid": f"adopted-{ordinal:04d}",
                    "name": f"Adopted {ordinal}",
                    "entity_type": "pattern" if global_limit and ordinal % 3 == 0 else "topic",
                    "group_id": client.group_id,
                    "name_embedding": [
                        math.cos(angle),
                        math.sin(angle),
                        *([0.0] * (EMBEDDING_DIM - 2)),
                    ],
                    "attributes": {
                        "embedding_metadata": {
                            field: stamp[field]
                            for field, exists in zip(fields, present, strict=True)
                            if exists
                        },
                        "user_metadata": {"ordinal": ordinal},
                    },
                    "content": f"Full adopted content {ordinal}",
                    "description": "Full adopted description",
                    "created_at": start + timedelta(seconds=ordinal % 7),
                    "revision": 9,
                }
            )
    incompatible = []
    for i in range(160 if global_limit else 80):
        for field in (*fields, "organization"):
            wrong_stamp = stamp.copy()
            if field in fields:
                wrong_stamp[field] = EMBEDDING_DIM - 1 if field == "dimensions" else "another"
            incompatible.append(
                {
                    "uuid": f"rejected-{field}-{i:04d}",
                    "name": "Rejected nearest vector",
                    "entity_type": "topic",
                    "group_id": "another-organization"
                    if field == "organization"
                    else client.group_id,
                    "name_embedding": query_vector,
                    "attributes": {"embedding_metadata": wrong_stamp},
                }
            )
    await client.execute_query(
        "INSERT INTO entity $rows;",
        rows=[
            *rows,
            *incompatible,
            {
                "uuid": "adopted-null-vector",
                "name": "Adopted null vector",
                "entity_type": "topic",
                "group_id": client.group_id,
                "name_embedding": None,
                "attributes": {"embedding_metadata": stamp},
            },
        ],
    )
    await client.execute_query(
        "UPSERT type::record($key) SET organization_id=$org, plane='graph', "
        "legacy_decision='adopt', legacy_metadata=$stamp;",
        key=embedding_state_key(client.group_id, "graph"),
        org=client.group_id,
        stamp=stamp,
    )

    async def embed_query(*_args: object, **_kwargs: object) -> list[list[float]]:
        return [query_vector]

    monkeypatch.setattr(provider, "embed_texts", embed_query)
    execute = client.execute_query
    exact_calls: list[tuple[str, dict[str, Any]]] = []

    async def observed(query: str, **params: Any) -> Any:
        if (global_limit or type_selection == "all-types") and params.get(
            "_query_label"
        ) == "entity.search.vector":
            # Force a real shortfall across full stamp and requested type cohorts.
            query = re.sub(r"<\|\d+,\s*(\d+)\|>", r"<|1, \1|>", query, count=1)
        result = await execute(query, **params)
        if params.get("_query_label") == "entity.search.vector.exact":
            exact_calls.append((query, params))
        return result

    monkeypatch.setattr(client, "execute_query", observed)
    found = await EntityManager(
        client, group_id=client.group_id, embedding_provider=provider
    )._vector_search(
        query="adopted completion",
        entity_types=types,
        limit=5,
    )
    expected = sorted(
        (row for row in rows if not requested or row["entity_type"] in requested),
        key=lambda row: (
            _cosine_score(row["name_embedding"], query_vector),
            row["created_at"],
            row["uuid"],
        ),
        reverse=True,
    )[:32]
    assert [entity.id for entity, _score in found] == [row["uuid"] for row in expected]
    for (entity, score), row in zip(found, expected, strict=True):
        assert score == pytest.approx(_cosine_score(row["name_embedding"], query_vector))
        assert entity.content == row["content"]
        assert entity.description == row["description"]
        assert entity.created_at == row["created_at"]
        assert entity.metadata["user_metadata"] == row["attributes"]["user_metadata"]
        assert entity.revision == 9
    assert len(exact_calls) == 1
    query, params = exact_calls[0]
    assert params["limit"] == 32
    if client._url != "memory://":
        plan = await execute(query.rstrip().removesuffix(";") + " EXPLAIN FULL;", **params)

        def nodes(value: object):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from nodes(child)
            elif isinstance(value, list):
                for child in value:
                    yield from nodes(child)

        scans = [node for node in nodes(plan) if node.get("operator") == "IndexScan"]
        assert len(scans) == 8 * (len(requested) or 1), plan
        assert all(
            node.get("attributes", {}).get("index")
            == ("idx_entity_typed_vector_space" if requested else "idx_entity_vector_space")
            for node in scans
        ), plan
        assert (
            sum(node["metrics"]["output_rows"] for node in scans)
            == sum(not requested or row["entity_type"] in requested for row in rows) + 1
        ), plan
        assert not any(node.get("operator") == "TableScan" for node in nodes(plan)), plan


@pytest.mark.parametrize("admit_unstamped", [False, True])
@pytest.mark.parametrize("overfetch", [0, 4])
async def test_entity_vector_completion_failure_retains_ann_rows(
    monkeypatch: pytest.MonkeyPatch, admit_unstamped: bool, overfetch: int
) -> None:
    from structlog.testing import capture_logs

    from sibyl_core.services.embedding_lane_readiness import LaneReadiness

    async def ready(**_kwargs: object) -> LaneReadiness:
        return LaneReadiness(run=True, reason="test", admit_unstamped=admit_unstamped)

    monkeypatch.setattr("sibyl_core.services.embedding_lane_readiness.vector_lane_readiness", ready)
    ann = [_entity_row("valid-ann", score=0.83), _entity_row("another-ann", score=0.74)]
    client = _ScriptedClient(
        "org-overfetch",
        {"entity.search.vector": ann, "entity.search.vector.overfetch": ann[:1]},
    )
    execute = client.execute_query

    async def fault(query: str, **params: object) -> list[dict[str, object]]:
        if params.get("_query_label") == "entity.search.vector.exact":
            raise RuntimeError("exact completion fault injection")
        return await execute(query, **params)

    monkeypatch.setattr(client, "execute_query", fault)
    manager = EntityManager(
        client, group_id=client.group_id, embedding_provider=_overfetch_provider("fault")
    )
    with capture_logs() as captured:
        found = await manager._vector_search(
            query="completion failure",
            entity_types=[EntityType.TOPIC],
            limit=5,
            knn_type_overfetch=overfetch,
        )
    assert [(entity.id, score) for entity, score in found] == [
        ("valid-ann", 0.83),
        ("another-ann", 0.74),
    ]
    assert [
        entry for entry in captured if entry["event"] == "entity_vector_search_completion_failed"
    ] == [
        {
            "event": "entity_vector_search_completion_failed",
            "log_level": "warning",
            "error_type": "RuntimeError",
            "ann_yield": 2,
            "candidate_limit": 32,
        }
    ]
    assert not any(entry["event"] == "entity_vector_search_failed" for entry in captured)


@pytest.mark.parametrize("admit_unstamped", [False, True], ids=["strict", "adopted"])
@pytest.mark.parametrize(
    "types",
    [
        (EntityType.TOPIC,),
        (EntityType.TOPIC, EntityType.PATTERN),
        (EntityType.TOPIC, EntityType.PATTERN, EntityType.TOPIC),
    ],
    ids=["single", "multiple", "duplicate"],
)
async def test_typed_vector_completion_bounds_wrong_type_growth(
    vector_completion_client: SurrealGraphClient,
    monkeypatch: pytest.MonkeyPatch,
    types: tuple[EntityType, ...],
    admit_unstamped: bool,
) -> None:
    from datetime import timedelta
    from itertools import product

    from sibyl_core.services.embedding_lane_readiness import LaneReadiness

    client = vector_completion_client
    provider = _overfetch_provider("typed-space-completion")
    stamp = provider.metadata.to_dict()
    fields = ("provider", "model", "dimensions")
    query_vector = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    start = datetime(2026, 9, 30, 12, tzinfo=UTC)
    rows = []
    for present in product((False, True), repeat=3):
        for entity_type in ("topic", "pattern"):
            for _ in range(4):
                ordinal = len(rows)
                angle = (ordinal // 4 + 1) / 50
                rows.append(
                    {
                        "uuid": f"typed-{ordinal:04d}",
                        "name": f"Typed {ordinal}",
                        "entity_type": entity_type,
                        "group_id": client.group_id,
                        "name_embedding": [
                            math.cos(angle),
                            math.sin(angle),
                            *([0.0] * (EMBEDDING_DIM - 2)),
                        ],
                        "attributes": {
                            "embedding_metadata": {
                                field: stamp[field]
                                for field, exists in zip(fields, present, strict=True)
                                if exists
                            },
                            "user_metadata": {"ordinal": ordinal},
                        },
                        "content": f"Full typed content {ordinal}",
                        "created_at": start + timedelta(seconds=ordinal % 7),
                        "revision": 9,
                    }
                )
    nulls = [
        {
            "uuid": f"typed-null-{kind}",
            "name": "Null vector",
            "entity_type": kind,
            "group_id": client.group_id,
            "name_embedding": None,
            "attributes": {"embedding_metadata": stamp},
        }
        for kind in ("topic", "pattern")
    ]
    await client.execute_query("INSERT INTO entity $rows;", rows=[*rows, *nulls])

    async def ready(**_kwargs: object) -> LaneReadiness:
        return LaneReadiness(run=True, reason="test", admit_unstamped=admit_unstamped)

    async def embed_query(*_args: object, **_kwargs: object) -> list[list[float]]:
        return [query_vector]

    monkeypatch.setattr("sibyl_core.services.embedding_lane_readiness.vector_lane_readiness", ready)
    monkeypatch.setattr(provider, "embed_texts", embed_query)
    execute = client.execute_query
    exact_calls: list[tuple[str, dict[str, Any]]] = []

    async def observed(query: str, **params: Any) -> Any:
        if params.get("_query_label") == "entity.search.vector":
            query = re.sub(r"<\|\d+,\s*(\d+)\|>", r"<|1, \1|>", query, count=1)
        result = await execute(query, **params)
        if params.get("_query_label") == "entity.search.vector.exact":
            exact_calls.append((query, params))
        return result

    monkeypatch.setattr(client, "execute_query", observed)
    requested = {kind.value for kind in types}
    eligible = [
        row
        for row in rows
        if row["entity_type"] in requested
        and (
            admit_unstamped
            or all(
                row["attributes"]["embedding_metadata"].get(field) == stamp[field]
                for field in fields
            )
        )
    ]
    expected = sorted(
        eligible,
        key=lambda row: (
            _cosine_score(row["name_embedding"], query_vector),
            row["created_at"],
            row["uuid"],
        ),
        reverse=True,
    )[:32]
    for growth in (320, 800):
        wrong = []
        for i in range(growth):
            present = tuple(bool((i // 10 >> j) & 1) for j in range(3))
            wrong.append(
                {
                    "uuid": f"wrong-type-{growth}-{i}",
                    "name": "Wrong type",
                    "entity_type": "note",
                    "group_id": client.group_id,
                    "name_embedding": query_vector,
                    "attributes": {
                        "embedding_metadata": {
                            field: stamp[field]
                            for field, exists in zip(fields, present, strict=True)
                            if exists
                        }
                    },
                }
            )
        await client.execute_query("INSERT INTO entity $rows;", rows=wrong)
        found = await EntityManager(
            client, group_id=client.group_id, embedding_provider=provider
        )._vector_search(query="typed completion", entity_types=types, limit=5)
        assert [entity.id for entity, _score in found] == [row["uuid"] for row in expected]
        assert len({entity.id for entity, _score in found}) == len(found)
        for (entity, score), row in zip(found, expected, strict=True):
            assert score == pytest.approx(_cosine_score(row["name_embedding"], query_vector))
            assert entity.content == row["content"]
            assert entity.created_at == row["created_at"]
            assert entity.metadata["user_metadata"] == row["attributes"]["user_metadata"]
            assert entity.revision == 9
        assert len(exact_calls) == (1 if growth == 320 else 2)
        query, params = exact_calls[-1]
        assert params["limit"] == 32
        assert "entity_type IN" not in query
        assert "'topic'" not in query and "'pattern'" not in query
        assert {
            value for key, value in params.items() if key.startswith("entity_type_")
        } == requested
        assert query.count("LIMIT $limit") == 1
        if client._url != "memory://":
            plan = await execute(query.rstrip().removesuffix(";") + " EXPLAIN FULL;", **params)

            def nodes(value: object):
                if isinstance(value, dict):
                    yield value
                    for child in value.values():
                        yield from nodes(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from nodes(child)

            scans = [node for node in nodes(plan) if node.get("operator") == "IndexScan"]
            assert len(scans) == len(requested) * (8 if admit_unstamped else 1), plan
            assert all(
                node["attributes"]["index"] == "idx_entity_typed_vector_space" for node in scans
            ), plan
            assert sum(node["metrics"]["output_rows"] for node in scans) == len(eligible) + len(
                requested
            ), plan
            assert not any(node.get("operator") == "TableScan" for node in nodes(plan)), plan


async def test_entity_typed_vector_space_index_upgrade_retains_sources_and_associations(
    vector_completion_client: SurrealGraphClient,
) -> None:
    client = vector_completion_client
    migrations = _graph_schema_migrations(url=client._url, group_id=client.group_id)
    assert await get_schema_version(client.execute_query, name="graph") == 35
    assert {"idx_entity_vector_space", "idx_entity_typed_vector_space", "idx_entity_embedding"} <= (
        await client.execute_query("INFO FOR TABLE entity;")
    )["indexes"].keys()
    historical = SurrealGraphClient(
        group_id="typed-vector-upgrade-" + uuid4().hex,
        url=client._url,
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    )
    try:
        for block in (
            ANALYZER_DEFINITIONS,
            NODE_DEFINITIONS.replace(ENTITY_TYPED_VECTOR_SPACE_INDEX_DEFINITIONS, ""),
            EDGE_DEFINITIONS,
        ):
            await historical.execute_query(render_surreal_compatible_sql(block, url=client._url))
        old = tuple(migration for migration in migrations if migration.version <= 34)
        await apply_schema_migrations(historical.execute_query, old, name="graph")
        assert await get_schema_version(historical.execute_query, name="graph") == 34
        assert (
            "idx_entity_typed_vector_space"
            not in (await historical.execute_query("INFO FOR TABLE entity;"))["indexes"]
        )
        await _seed_entities(
            historical, 12, stamp=_overfetch_provider("typed-upgrade").metadata.to_dict()
        )
        await historical.execute_query(
            "CREATE memory_derivations CONTENT $association;",
            association={
                "organization_id": historical.group_id,
                "target_kind": "graph_entity",
                "target_id": "knn_pool_0000",
                "body_sha256": "b" * 64,
                "principal_id": "proof-owner",
                "authority_ceiling": {},
                "observations": [],
                "active": True,
            },
        )
        before = {}
        for table in ("entity", "source_states", "memory_derivations"):
            before[table] = await historical.execute_query(f"SELECT * FROM {table} ORDER BY id;")
        applied = await apply_schema_migrations(historical.execute_query, migrations, name="graph")
        assert [migration.version for migration in applied] == [35]
        assert await get_schema_version(historical.execute_query, name="graph") == 35
        assert {
            "idx_entity_vector_space",
            "idx_entity_typed_vector_space",
            "idx_entity_embedding",
        } <= (await historical.execute_query("INFO FOR TABLE entity;"))["indexes"].keys()
        for table, snapshot in before.items():
            assert await historical.execute_query(f"SELECT * FROM {table} ORDER BY id;") == snapshot
        assert before["source_states"] and before["memory_derivations"]
        assert (
            await apply_schema_migrations(historical.execute_query, migrations, name="graph") == []
        )
    finally:
        await historical.execute_query(f"REMOVE NAMESPACE {historical.namespace};")
        assert (
            historical.namespace
            not in (await historical.execute_query("INFO FOR ROOT;"))["namespaces"]
        )
        await historical.close()
