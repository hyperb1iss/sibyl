from __future__ import annotations

from pathlib import Path

import pytest
from tools.perf.surreal_vector_index import (
    TARGET_ORGANIZATION_ID,
    TARGET_SOURCE_ID,
    ChunkRow,
    VectorBenchConfig,
    build_index_plans,
    choose_index,
    config_from_args,
    cosine_similarity,
    exact_top_k,
    percentile,
    recall_at,
    surreal_identifier,
)

PERCENTILE_RESULT = 25.0


def _config(tmp_path: Path, *, include_diskann: bool = True) -> VectorBenchConfig:
    return VectorBenchConfig(
        rows=90,
        dimensions=4,
        queries=3,
        limit=2,
        candidate_limit=10,
        ef=40,
        batch_size=10,
        seed=7,
        content_bytes=64,
        output_path=tmp_path / "bench.json",
        cache_dir=tmp_path / "cache",
        run_id="20260530-test",
        url=None,
        namespace="sibyl_vector_bench_test",
        database="bench",
        include_diskann=include_diskann,
        hnsw_efc=150,
        hnsw_m=12,
        diskann_degree=16,
        diskann_l_build=64,
    )


def test_percentile_interpolates_latency() -> None:
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.5) == PERCENTILE_RESULT
    assert percentile([], 0.95) is None


def test_exact_top_k_and_recall_use_org_source_scope() -> None:
    rows = [
        ChunkRow("target-a", TARGET_ORGANIZATION_ID, TARGET_SOURCE_ID, "doc-a", 0, "", [1, 0]),
        ChunkRow("target-b", TARGET_ORGANIZATION_ID, TARGET_SOURCE_ID, "doc-b", 1, "", [0, 1]),
        ChunkRow("other-org", "org-b", TARGET_SOURCE_ID, "doc-c", 2, "", [1, 0]),
        ChunkRow("other-source", TARGET_ORGANIZATION_ID, "source-b", "doc-d", 3, "", [1, 0]),
    ]

    result = exact_top_k(
        rows,
        [1, 0],
        organization_id=TARGET_ORGANIZATION_ID,
        source_id=TARGET_SOURCE_ID,
        limit=2,
    )

    assert result == ["target-a", "target-b"]
    assert recall_at(result, ["target-a"]) == pytest.approx(0.5)
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)


def test_index_plans_match_current_hnsw_and_diskann_syntax(tmp_path: Path) -> None:
    plans = {plan.name: plan for plan in build_index_plans(_config(tmp_path))}

    assert "HNSW DIMENSION 4 DIST COSINE TYPE F32 EFC 150 M 12" in (plans["hnsw"].definition)
    assert "DISKANN DIMENSION 4 DIST COSINE TYPE F32 DEGREE 16 L_BUILD 64" in (
        plans["diskann"].definition
    )


def test_index_plans_can_skip_diskann(tmp_path: Path) -> None:
    plans = build_index_plans(_config(tmp_path, include_diskann=False))

    assert [plan.name for plan in plans] == ["hnsw"]


def test_choose_index_keeps_hnsw_when_diskann_unsupported() -> None:
    decision = choose_index(
        {
            "hnsw": {
                "supported": True,
                "query_summary": {
                    "recall_at_k": 1.0,
                    "latency_ms": {"p95": 10.0},
                },
                "index_build_ms": 100.0,
            },
            "diskann": {
                "supported": False,
                "error": "Parse error: Unexpected token `DISKANN`",
            },
        }
    )

    assert decision["recommendation"] == "keep_hnsw"
    assert "unavailable" in decision["reason"]


def test_choose_index_requires_diskann_to_win() -> None:
    decision = choose_index(
        {
            "hnsw": {
                "supported": True,
                "query_summary": {
                    "recall_at_k": 1.0,
                    "latency_ms": {"p95": 10.0},
                },
                "index_build_ms": 100.0,
            },
            "diskann": {
                "supported": True,
                "query_summary": {
                    "recall_at_k": 0.9,
                    "latency_ms": {"p95": 9.0},
                },
                "index_build_ms": 90.0,
            },
        }
    )

    assert decision["recommendation"] == "keep_hnsw"


def test_surreal_identifier_is_safe_for_generated_tables() -> None:
    assert surreal_identifier("20260530-test run") == "run_20260530_test_run"


def test_config_paths_follow_cli_run_id_when_not_overridden() -> None:
    config = config_from_args(["--run-id", "fixed-run"])

    assert config.output_path.name == "surreal_vector_index_fixed-run.json"
    assert config.cache_dir.name == "fixed-run"
