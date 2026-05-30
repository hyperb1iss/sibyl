from __future__ import annotations

from sibyl_core.backends.surreal.query_plan_probes import (
    analyze_query_plan,
    build_hot_query_plan_probes,
)


def test_hot_query_plan_probes_match_search_query_shapes() -> None:
    probes = build_hot_query_plan_probes(
        org_id="org-123",
        graph_query_embedding=[0.0, 0.0, 0.0, 0.0],
        content_query_embedding=[0.0, 0.0, 0.0, 0.0],
        query_text="native graph",
        source_ids=("source-123",),
        project_ids=("project-123",),
        entity_types=("task",),
        node_types=("task",),
        limit=2,
        graph_knn_effort=96,
    )

    by_name = {probe.name: probe for probe in probes}
    assert set(by_name) == {
        "graph.entity_vector_search",
        "context.node_vector_search",
        "context.edge_vector_search",
        "content.rag_chunk_vector_search",
        "content.hybrid_lexical_search",
    }
    assert all(probe.query.strip().endswith("EXPLAIN FULL;") for probe in probes)
    assert (
        "name_embedding <|32, 96|> $query_embedding" in by_name["graph.entity_vector_search"].query
    )
    assert "entity_type IN $entity_types" in by_name["graph.entity_vector_search"].query
    assert (
        "name_embedding <|2, 96|> $query_embedding" in by_name["context.node_vector_search"].query
    )
    assert "project_id IN $project_ids" in by_name["context.node_vector_search"].query
    assert (
        "fact_embedding <|2, 96|> $query_embedding" in by_name["context.edge_vector_search"].query
    )
    assert (
        "embedding <|10, 40|> $query_embedding" in by_name["content.rag_chunk_vector_search"].query
    )
    assert "content @0@ $search_query" in by_name["content.hybrid_lexical_search"].query
    assert by_name["content.hybrid_lexical_search"].params["source_ids"] == ["source-123"]
    assert by_name["content.hybrid_lexical_search"].params["search_query"] == "native graph"


def test_query_plan_analysis_records_indexes_and_scan_operations() -> None:
    analysis = analyze_query_plan(
        [
            {
                "operation": "Iterate Index",
                "detail": {"plan": {"index": "idx_entity_embedding"}, "count": 7},
            },
            {"operation": "Iterate Table", "table": "entity"},
        ],
        expected_indexes=("idx_entity_embedding",),
        max_executed_rows=5,
    )

    assert analysis.used_indexes == ("idx_entity_embedding",)
    assert analysis.uses_expected_index is True
    assert analysis.scan_operations == ("Iterate Table: entity",)
    assert analysis.executed_row_counts == (7,)
    assert analysis.max_executed_rows == 7
    assert analysis.scans_more_than_expected is True


def test_probe_payload_summarizes_large_vectors() -> None:
    probe = build_hot_query_plan_probes(
        org_id="org-123",
        graph_query_embedding=[0.0] * 12,
        content_query_embedding=[0.0] * 12,
    )[0]

    payload = probe.to_dict()

    assert payload["params"]["query_embedding"] == {"type": "vector", "length": 12}
