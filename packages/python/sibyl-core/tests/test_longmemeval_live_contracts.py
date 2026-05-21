from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]


def _load_script(relative_path: str) -> ModuleType:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_longmemeval_report_uses_graph_embedding_runtime(monkeypatch) -> None:
    module = _load_script("benchmarks/longmemeval_live.py")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "1024")
    monkeypatch.setenv("SIBYL_OPENAI_API_KEY", "test-key")

    metadata = module._graph_embedding_runtime_metadata()

    assert metadata["embedding_provider"] == "openai"
    assert metadata["embedding_model"] == "text-embedding-3-small"
    assert metadata["embedding_dimensions"] == 1024
    assert metadata["embedding_provider_status"] == "enabled"
    assert "native vector" in metadata["retrieval_semantics"]
    assert metadata["vector_search_surface"] == (
        "entity.name_embedding KNN via NativeEntityManager.search"
    )


def test_longmemeval_preflight_detects_vector_search_surface() -> None:
    module = _load_script("benchmarks/preflight/longmemeval_live_contract_probe.py")

    semantics = module._source_semantics()

    graph_function = semantics["api_search_graph_function"]
    assert graph_function["uses_fulltext_scores"] is True
    assert graph_function["uses_knn_vector"] is True
    assert graph_function["uses_embedding_provider"] is True


def test_longmemeval_live_checkpoints_partial_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_script("benchmarks/longmemeval_live.py")
    dataset = tmp_path / "longmemeval_s_cleaned.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "question_id": "q1",
                    "question_type": "single-session-user",
                    "question": "What changed?",
                    "answer_session_ids": ["s1"],
                    "haystack_sessions": [],
                    "haystack_session_ids": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "result.json"
    case_result = {
        "case_index": 0,
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What changed?",
        "answer_session_ids": ["s1"],
        "ranked_session_ids": ["s1"],
        "ranked_results": [],
        "cross_question_result_count": 0,
        "created_entity_count": 1,
        "chunked_session_count": 0,
        "memory_extraction_job_count": 0,
        "memory_extraction_wait_ms": 0,
        "memory_extraction": {},
        "hit@5": 1.0,
        "legacy_recall@5": 1.0,
        "recall@5": 1.0,
        "ndcg@5": 1.0,
        "hit@10": 1.0,
        "legacy_recall@10": 1.0,
        "recall@10": 1.0,
        "ndcg@10": 1.0,
    }
    seen_statuses: list[str] = []

    async def fake_run_cases(*_: object, **kwargs: object) -> list[dict[str, object]]:
        initial = json.loads(output.read_text(encoding="utf-8"))
        seen_statuses.append(initial["completion_status"])
        assert initial["completed_questions"] == 0
        await kwargs["on_progress"]([case_result])
        partial = json.loads(output.read_text(encoding="utf-8"))
        seen_statuses.append(partial["completion_status"])
        assert partial["completed_questions"] == 1
        return [case_result]

    monkeypatch.setattr(module, "_run_cases", fake_run_cases)

    report = asyncio.run(
        module.run_benchmark(
            dataset,
            api_url="http://localhost:3334/api",
            allow_localhost=True,
            k_values=[5, 10],
            command=["longmemeval_live.py"],
            verify_sha256=False,
            output_path=output,
        )
    )

    saved = json.loads(output.read_text(encoding="utf-8"))
    assert seen_statuses == ["partial", "partial"]
    assert report["completion_status"] == "complete"
    assert report["runtime"]["memory_enrichment_consistency"] == "async"
    assert report["runtime"]["memory_projection_consistency"] == "async"
    assert report["runtime"]["memory_extraction_consistency"] == "async"
    assert report["runtime"]["wait_for_memory_projection"] is False
    assert report["dataset"]["memory_enrichment_consistency"] == "async"
    assert report["dataset"]["memory_projection_consistency"] == "async"
    assert saved["completion_status"] == "complete"
    assert saved["completed_questions"] == 1


def test_longmemeval_ingest_only_waits_for_projection_when_requested(monkeypatch) -> None:
    module = _load_script("benchmarks/longmemeval_live.py")
    entry = {"question_id": "q1"}
    document = SimpleNamespace(
        text="User: I bought a Samsung TV for the den.",
        session_id="s1",
        timestamp="2026/01/03 12:00",
    )
    wait_calls: list[list[str]] = []

    async def fake_post_json(*_: object, **__: object) -> dict[str, object]:
        return {
            "entities": [{"id": "session_one"}],
            "background_jobs": {
                "memory_projection": {
                    "status": "queued",
                    "job_ids": ["project_memory:one"],
                    "queued_sources": 1,
                    "skipped_sources": 0,
                }
            },
        }

    async def fake_wait_for_jobs(
        _client: object,
        job_ids: list[str],
        **_: object,
    ) -> list[dict[str, object]]:
        wait_calls.append(job_ids)
        return [{"result": {"sources": 1, "projected_entities": 1}}]

    monkeypatch.setattr(module, "build_longmemeval_corpus", lambda *_args, **_kwargs: [document])
    monkeypatch.setattr(module, "_post_json", fake_post_json)
    monkeypatch.setattr(module, "_wait_for_jobs", fake_wait_for_jobs)

    asyncio.run(
        module._ingest_haystack(
            SimpleNamespace(),
            entry=entry,
            run_id="run",
            case_index=0,
            corpus_text_policy="user-and-assistant-turns-v1",
            wait_for_memory_projection=False,
            wait_for_memory_extraction=False,
            memory_projection_timeout_seconds=1,
            memory_extraction_timeout_seconds=1,
        )
    )
    assert wait_calls == []

    asyncio.run(
        module._ingest_haystack(
            SimpleNamespace(),
            entry=entry,
            run_id="run",
            case_index=0,
            corpus_text_policy="user-and-assistant-turns-v1",
            wait_for_memory_projection=True,
            wait_for_memory_extraction=False,
            memory_projection_timeout_seconds=1,
            memory_extraction_timeout_seconds=1,
        )
    )
    assert wait_calls == [["project_memory:one"]]
