from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from shutil import which
from types import ModuleType
from typing import Any

import httpx
import pytest
import yaml
from tools.bench import eval_gate

from sibyl_core.evals.longmemeval import LongMemEvalCorpusDocument

PREFERENCE_CASE_INDEX = 2
EXPECTED_CREATED_ENTITIES = 3
EXPECTED_CHUNKED_ENTITIES = 2
EXPECTED_EXTRACTION_QUEUE_DEPTH = 3
EXPECTED_EXTRACTION_TOKENS = 128
EXPECTED_EXTRACTED_ENTITIES = 2
EXPECTED_PROJECTION_EXTRACTED = 3
EXPECTED_PROJECTION_PROJECTED_ENTITIES = 2
EXPECTED_PROJECTION_RELATIONSHIPS = 2
EXPECTED_PROJECTION_SKIPPED = 1


class _BlankSecret:
    def get_secret_value(self) -> str:
        return ""


def test_eval_workflow_full_run_forces_memory_extraction_off() -> None:
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "eval.yml").read_text(
        encoding="utf-8"
    )
    smoke_job = workflow.split("  longmemeval-live-smoke:", 1)[1].split(
        "  longmemeval-local-smoke:",
        1,
    )[0]
    local_job = workflow.split("  longmemeval-local-smoke:", 1)[1].split(
        "  longmemeval-local-vs-openai:",
        1,
    )[0]
    comparison_job = workflow.split("  longmemeval-local-vs-openai:", 1)[1].split(
        "  longmemeval-live-full:",
        1,
    )[0]
    full_job = workflow.split("  longmemeval-live-full:", 1)[1]

    assert "Enable queued LLM memory extraction during LongMemEval smoke only" in workflow
    assert "run_longmemeval_qa:" in workflow
    assert "LONGMEMEVAL_QA_MODE:" in workflow
    assert "LONGMEMEVAL_QA_READER_MODEL: gpt-4o" in workflow
    assert "LONGMEMEVAL_QA_JUDGE_MODEL: gpt-5.2" in workflow
    assert "pull_request:" in workflow
    assert (
        "SIBYL_AUTO_EXTRACT_ENTITIES: ${{ inputs.longmemeval_auto_extract_entities || false }}"
    ) in smoke_job
    assert "if: github.event_name != 'pull_request'" in smoke_job
    assert 'SIBYL_AUTO_EXTRACT_ENTITIES: "false"' in full_job
    assert "SIBYL_LLM_MEMORY_PROVIDER" not in full_job
    assert "--wait-for-memory-extraction" not in full_job
    assert "Full LongMemEval must run with SIBYL_AUTO_EXTRACT_ENTITIES=false." in full_job
    assert '--metadata auto_extract_entities="${SIBYL_AUTO_EXTRACT_ENTITIES}"' in full_job
    assert "inputs.run_longmemeval_qa" in full_job
    assert "--qa-mode model" in full_job
    assert "--qa-reader-model" in full_job
    assert "--qa-judge-model" in full_job
    assert "--require-qa" in full_job
    assert "--require-runtime qa_mode=model" in full_job
    assert "pinned-longmemeval-s-qa.json" in full_job
    assert "Claim-bearing QA requires a complete matching pinned baseline." in full_job
    assert "bootstrap_longmemeval_qa_baseline:" in workflow
    assert "Guard QA comparison contract before spending" in full_job
    assert "--baseline-metric qa_accuracy" in full_job
    assert "--max-regression qa_accuracy=0.01" in full_job
    assert "--qa-mode model" not in smoke_job
    assert "--qa-mode model" not in local_job
    assert "--require-runtime embedding_provider=openai" in smoke_job
    assert "--require-runtime embedding_provider=openai" in full_job
    assert "--require-accounting" in smoke_job
    assert "--require-accounting" in full_job
    assert 'SIBYL_LOCAL_AUTH_ENABLED: "true"' in smoke_job
    assert 'SIBYL_LOCAL_AUTH_ENABLED: "true"' in local_job
    assert 'SIBYL_LOCAL_AUTH_ENABLED: "true"' in full_job
    assert "longmemeval-live-smoke" in comparison_job


def test_eval_workflow_has_pr_safe_local_embedding_slice() -> None:
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "eval.yml").read_text(
        encoding="utf-8"
    )
    local_job = workflow.split("  longmemeval-local-smoke:", 1)[1].split(
        "  longmemeval-local-vs-openai:",
        1,
    )[0]
    moon = (Path(__file__).parents[2] / "moon.yml").read_text(encoding="utf-8")

    assert '"uv.lock"' in workflow
    assert '"pyproject.toml"' in workflow
    assert '".github/actions/start-surrealdb/**"' in workflow
    assert '"packages/python/sibyl-core/pyproject.toml"' in workflow
    assert '"apps/api/pyproject.toml"' in workflow
    assert '"tools/tests/test_compare_eval_reports.py"' in workflow
    assert '"tools/tests/test_context_pack_eval_script.py"' in workflow
    assert "bench-longmemeval-live-local:" in moon
    assert "uv run --with sentence-transformers==6.0.0 python benchmarks/longmemeval_live.py" in (
        moon
    )
    assert "LONGMEMEVAL_LOCAL_SMOKE_LIMIT:" in workflow
    assert "github.event_name == 'pull_request' && '10'" in workflow
    assert "SIBYL_GRAPH_EMBEDDING_PROVIDER: local" in local_job
    assert "SIBYL_GRAPH_EMBEDDING_MODEL: sentence-transformers/all-MiniLM-L6-v2" in local_job
    assert 'SIBYL_GRAPH_EMBEDDING_DIMENSIONS: "384"' in local_job
    assert "SIBYL_OPENAI_API_KEY" not in local_job
    assert "secrets.OPENAI_API_KEY" not in local_job
    assert "for i in {1..180}; do" in local_job
    assert "uv run --with sentence-transformers==6.0.0 sibyld serve" in local_job
    assert "uv run --with sentence-transformers==6.0.0 sibyld worker" in local_job
    assert "moon run bench-longmemeval-live-local" in local_job
    assert "--metadata comparison_peer=longmemeval-live-smoke" in local_job
    assert "--metadata embedding_variant=local-all-MiniLM-L6-v2" in local_job
    assert "moon run bench-gate -- .moon/cache/evals/longmemeval_local_smoke.json" in local_job
    assert "--require-runtime embedding_provider=local" in local_job
    assert "--require-runtime embedding_provider_status=enabled" in local_job
    assert "--require-runtime embedding_cache_namespace=graph" in local_job
    assert "--require-accounting" in local_job
    assert "graph_embeddings_disabled.*provider=local" in local_job


def test_eval_workflow_compares_local_and_openai_smoke_receipts() -> None:
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "eval.yml").read_text(
        encoding="utf-8"
    )
    comparison_job = workflow.split("  longmemeval-local-vs-openai:", 1)[1].split(
        "  longmemeval-live-full:",
        1,
    )[0]

    assert "if: github.event_name != 'pull_request'" in comparison_job
    assert "longmemeval-live-smoke" in comparison_job
    assert "longmemeval-local-smoke" in comparison_job
    assert "actions/download-artifact@v8" in comparison_job
    assert "longmemeval-live-smoke-${{ github.sha }}" in comparison_job
    assert "longmemeval-local-smoke-${{ github.sha }}" in comparison_job
    assert (
        "moon run bench-gate -- .moon/cache/evals/local/longmemeval_local_smoke.json"
        in comparison_job
    )
    assert "--baseline .moon/cache/evals/openai/longmemeval_live_smoke.json" in comparison_job
    assert "--baseline-metric recall@5" in comparison_job
    assert "--baseline-metric ndcg@5" in comparison_job
    assert "moon run bench-compare-reports" in comparison_job
    assert "longmemeval_local_vs_openai_comparison.txt" in comparison_job


def _load_live_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_live.py"
    spec = importlib.util.spec_from_file_location("longmemeval_live", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_response(request: httpx.Request, payload: dict[str, Any], status_code: int = 200):
    return httpx.Response(status_code, json=payload, request=request)


def _bulk_create_fixture_entities(
    state: dict[str, Any], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    created = []
    for entity_payload in payload["entities"]:
        entity = {
            "id": f"entity-{len(state['entities'])}",
            "entity_type": entity_payload["entity_type"],
            "content": entity_payload["content"],
            "metadata": entity_payload["metadata"],
        }
        state["entities"].append(entity)
        created.append(entity)
    return created


def _assert_question_search_payload(module: ModuleType, payload: dict[str, Any]) -> None:
    assert payload["boost_recent"] is True
    assert payload["reference_time"] == "2026/01/03 12:00"
    assert payload["limit"] == module.DEFAULT_DIAGNOSTIC_SEARCH_LIMIT


def _assert_memory_extraction_stats(report: dict[str, Any]) -> None:
    assert report["overall"]["memory_extraction_queued_sources"] == float(EXPECTED_CREATED_ENTITIES)
    assert report["overall"]["memory_extraction_skipped_sources"] == 0.0
    assert report["overall"]["memory_extraction_queue_depth_max"] == float(
        EXPECTED_EXTRACTION_QUEUE_DEPTH
    )
    assert report["overall"]["memory_extraction_estimated_input_tokens"] == float(
        EXPECTED_EXTRACTION_TOKENS
    )
    assert report["overall"]["memory_extraction_projected_entities"] == 1.0
    assert report["overall"]["memory_extraction_relationships"] == 1.0
    assert report["case_results"][0]["memory_extraction"] == {
        "batches": 1,
        "job_count": 1,
        "job_result_count": 1,
        "queued_sources": EXPECTED_CREATED_ENTITIES,
        "skipped_sources": 0,
        "queue_depth_max": EXPECTED_EXTRACTION_QUEUE_DEPTH,
        "estimated_input_tokens": EXPECTED_EXTRACTION_TOKENS,
        "sources": EXPECTED_CREATED_ENTITIES,
        "extracted_entities": EXPECTED_EXTRACTED_ENTITIES,
        "projected_entities": 1,
        "relationships": 1,
        "errors": 0,
        "projection_errors": 0,
        "statuses": {"queued": 1},
        "reasons": {},
    }


def _assert_memory_projection_stats(report: dict[str, Any]) -> None:
    assert report["overall"]["memory_projection_job_count"] == 1.0
    assert report["overall"]["memory_projection_queued_sources"] == float(EXPECTED_CREATED_ENTITIES)
    assert report["overall"]["memory_projection_skipped_sources"] == 0.0
    assert report["overall"]["memory_projection_extracted"] == float(EXPECTED_PROJECTION_EXTRACTED)
    assert report["overall"]["memory_projection_projected_entities"] == float(
        EXPECTED_PROJECTION_PROJECTED_ENTITIES
    )
    assert report["overall"]["memory_projection_relationships"] == float(
        EXPECTED_PROJECTION_RELATIONSHIPS
    )
    assert report["overall"]["memory_projection_skipped"] == float(EXPECTED_PROJECTION_SKIPPED)
    assert report["case_results"][0]["memory_projection"] == {
        "batches": 1,
        "job_count": 1,
        "job_result_count": 1,
        "queued_sources": EXPECTED_CREATED_ENTITIES,
        "skipped_sources": 0,
        "sources": EXPECTED_CREATED_ENTITIES,
        "extracted": EXPECTED_PROJECTION_EXTRACTED,
        "projected_entities": EXPECTED_PROJECTION_PROJECTED_ENTITIES,
        "relationships": EXPECTED_PROJECTION_RELATIONSHIPS,
        "skipped": EXPECTED_PROJECTION_SKIPPED,
        "errors": 0,
        "statuses": {"queued": 1},
    }


def _assert_gate_valid_report(module: ModuleType, report: dict[str, Any]) -> None:
    assert report["schema_version"] == "longmemeval-live-v1"
    assert report["mode"] == "hybrid"
    assert report["runtime"]["embedding_provider"] == "disabled"
    assert report["runtime"]["embedding_dimensions"] == 0
    assert report["runtime"]["embedding_cache_namespace"] == "not-applicable"
    assert report["runtime"]["entity_content_projection_policy"] == (
        module.ENTITY_CONTENT_PROJECTION_POLICY
    )
    assert report["runtime"]["sample_strategy"] == module.DEFAULT_SAMPLE_STRATEGY
    assert report["runtime"]["diagnostic_search_limit"] == module.DEFAULT_DIAGNOSTIC_SEARCH_LIMIT
    assert report["runtime"]["wait_for_memory_extraction"] is True
    assert report["dataset"]["corpus_text_policy"] == module.CORPUS_TEXT_POLICY
    assert report["dataset"]["sample_strategy"] == module.DEFAULT_SAMPLE_STRATEGY
    assert report["dataset"]["diagnostic_search_limit"] == module.DEFAULT_DIAGNOSTIC_SEARCH_LIMIT
    assert report["dataset"]["wait_for_memory_extraction"] is True
    assert report["dataset"]["selected_case_indices"] == [0]
    assert report["dataset"]["entity_content_projection_policy"] == (
        module.ENTITY_CONTENT_PROJECTION_POLICY
    )
    assert report["overall"]["hit@1"] == 1.0
    assert report["overall"]["recall@1"] == 1.0
    assert report["overall"]["cross_question_result_count"] == 0.0
    assert report["overall"]["created_entity_count"] == float(EXPECTED_CREATED_ENTITIES)
    assert report["overall"]["chunked_session_count"] == 1.0
    assert report["overall"]["memory_projection_job_count"] == 1.0
    assert report["overall"]["memory_extraction_job_count"] == 1.0
    assert report["overall"]["latency_p50_ms"] >= 0.0
    assert report["overall"]["latency_p95_ms"] >= 0.0
    assert report["overall"]["full_context_baseline_estimated_tokens"] > 0.0
    assert report["overall"]["embedding_call_count"] == float(EXPECTED_CREATED_ENTITIES + 2)
    assert report["accounting"]["schema_version"] == "sibyl-eval-accounting-v1"
    assert report["accounting"]["latency"]["p50_ms"] >= 0.0
    assert report["accounting"]["latency"]["p95_ms"] >= 0.0
    assert report["accounting"]["tokens"]["estimated_input_tokens"] > 0.0
    assert report["accounting"]["embedding"]["calls"] == 0
    assert report["accounting"]["cost"]["estimated_total_usd"] == 0.0
    assert report["case_results"][0]["ranked_session_ids"] == ["s2", "s1"]
    assert report["case_results"][0]["answer_ranks"] == [{"session_id": "s2", "rank": 1}]
    assert report["case_results"][0]["missed_answer_session_ids"] == []
    assert report["case_results"][0]["created_entity_count"] == EXPECTED_CREATED_ENTITIES
    assert report["case_results"][0]["chunked_session_count"] == 1
    assert report["case_results"][0]["full_context_baseline_estimated_tokens"] > 0.0
    assert report["case_results"][0]["readiness_search_attempt_count"] == 1
    assert report["diagnostics"]["case_gap_count"] == 0


def _assert_fixture_qa_report(report: dict[str, Any]) -> None:
    assert report["qa"]["schema_version"] == "sibyl-longmemeval-s-qa-v1"
    assert report["qa"]["mode"] == "fixture"
    assert report["qa"]["enabled"] is True
    assert report["qa"]["reader_prompt_id"] == "sibyl-longmemeval-reader-v1"
    assert report["qa"]["judge_prompt_id"] == "sibyl-longmemeval-judge-v1"
    assert report["qa"]["rubric_id"] == "longmemeval-s-answer-correctness-v1"
    assert report["runtime"]["qa_mode"] == "fixture"
    assert report["runtime"]["qa_reader_model"] == "gpt-4o"
    assert report["runtime"]["qa_judge_model"] == "gpt-5.2"
    assert report["overall"]["qa_evaluated_count"] == 1.0
    assert report["overall"]["qa_correct_count"] == 1.0
    assert report["overall"]["qa_accuracy"] == 1.0
    assert report["overall"]["qa_mean_score"] == 1.0
    assert report["overall"]["qa_latency_ms"] >= 0.0
    assert report["overall"]["reader_estimated_input_tokens"] > 0.0
    assert report["overall"]["judge_estimated_input_tokens"] > 0.0
    assert report["accounting"]["reader"]["estimated_input_tokens"] > 0.0
    assert report["accounting"]["reader"]["estimated_output_tokens"] > 0.0
    assert report["accounting"]["reader"]["cost_basis"] == "deterministic-fixture-no-provider-cost"
    assert report["accounting"]["judge"]["estimated_input_tokens"] > 0.0
    assert report["accounting"]["judge"]["estimated_output_tokens"] > 0.0
    assert report["accounting"]["judge"]["cost_basis"] == "deterministic-fixture-no-provider-cost"
    assert report["accounting"]["tokens"]["estimated_output_tokens"] > 0.0

    case_qa = report["case_results"][0]["qa"]
    assert case_qa["evaluated"] is True
    assert case_qa["correct"] is True
    assert case_qa["score"] == 1.0
    assert case_qa["context_session_ids"] == ["s2", "s1"]
    assert case_qa["answer_session_ids"] == ["s2"]
    assert "markers" in case_qa["generated_answer"]
    assert "markers" in case_qa["reference_answer"]
    assert eval_gate.evaluate_report(report, profile="ai-memory", require_qa=True) == []


def _assert_chunked_entities(
    module: ModuleType,
    state: dict[str, Any],
) -> None:
    assert max(len(entity["content"]) for entity in state["entities"]) <= (
        module.ENTITY_CONTENT_MAX_CHARS
    )
    chunked_entities = [
        entity
        for entity in state["entities"]
        if entity["metadata"]["longmemeval_session_id"] == "s2"
    ]
    assert len(chunked_entities) == EXPECTED_CHUNKED_ENTITIES
    assert [entity["metadata"]["longmemeval_chunk_index"] for entity in chunked_entities] == [0, 1]
    assert {entity["metadata"]["longmemeval_chunk_count"] for entity in chunked_entities} == {
        EXPECTED_CHUNKED_ENTITIES
    }


def test_longmemeval_live_refuses_localhost_without_explicit_allow() -> None:
    module = _load_live_module()

    with pytest.raises(module.LongMemEvalLiveError, match="Refusing to run"):
        module.validate_target("http://localhost:3334/api", allow_localhost=False)

    module.validate_target("http://localhost:3334/api", allow_localhost=True)


def _native_context_response(request: httpx.Request, expected_budget: int) -> httpx.Response:
    assert request.url.path == "/api/context/pack"
    payload = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer fixture-access-token"
    assert payload["goal"] == "What did I buy?"
    assert payload["markdown_token_budget"] == expected_budget
    assert payload["record_exposure"] is False
    assert "answer" not in payload
    return _json_response(
        request,
        {
            "markdown": "# Native compiled context\nA native-only fact, with its date: 2026/01/02.",
            "total_items": 1,
            "sections": [],
            "usage_metadata": {},
        },
    )


@pytest.mark.parametrize("qa_context_arm", ["historical-prefix-v1", "native-context-v1"])
def test_longmemeval_live_builds_gate_valid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qa_context_arm: str,
) -> None:
    module = _load_live_module()
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "openai")
    monkeypatch.delenv("SIBYL_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(module.settings, "openai_api_key", _BlankSecret())
    data_path = tmp_path / "longmemeval_s_cleaned.json"
    data_path.write_text(
        json.dumps(
            [
                {
                    "question_id": "q1",
                    "question_type": "single-session-user",
                    "question": "What did I buy?",
                    "question_date": "2026/01/03 12:00",
                    "answer_session_ids": ["s2"],
                    "haystack_session_ids": ["s1", "s2"],
                    "haystack_dates": ["2026/01/01", "2026/01/02"],
                    "haystack_sessions": [
                        [{"role": "user", "content": "I bought pencils."}],
                        [
                            {
                                "role": "user",
                                "content": "I bought markers. " + ("x" * 50_000),
                            }
                        ],
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    state: dict[str, Any] = {"token": None, "entities": [], "jobs": {}}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return _json_response(request, {"status": "ok"})
        if path == "/api/auth/local/signup":
            state["token"] = "fixture-access-token"  # noqa: S105
            return _json_response(
                request,
                {
                    "access_token": state["token"],
                    "organization": {"id": "org-q1", "slug": "org-q1"},
                },
                status_code=201,
            )
        if path == "/api/entities/bulk":
            payload = json.loads(request.content)
            created = _bulk_create_fixture_entities(state, payload)
            projection_job_id = f"project-{len(state['jobs'])}"
            state["jobs"][projection_job_id] = {
                "job_id": projection_job_id,
                "function": "project_memory_batch",
                "status": "complete",
                "result": {
                    "sources": len(created),
                    "extracted": EXPECTED_PROJECTION_EXTRACTED,
                    "projected_entities": EXPECTED_PROJECTION_PROJECTED_ENTITIES,
                    "relationships": EXPECTED_PROJECTION_RELATIONSHIPS,
                    "skipped": EXPECTED_PROJECTION_SKIPPED,
                    "errors": [],
                },
                "error": None,
            }
            extraction_job_id = f"extract-{len(state['jobs'])}"
            state["jobs"][extraction_job_id] = {
                "job_id": extraction_job_id,
                "function": "extract_memory_entities",
                "status": "complete",
                "result": {
                    "estimated_input_tokens": EXPECTED_EXTRACTION_TOKENS,
                    "sources": len(created),
                    "extracted_entities": EXPECTED_EXTRACTED_ENTITIES,
                    "projected_entities": 1,
                    "relationships": 1,
                    "errors": [],
                    "projection_errors": [],
                },
                "error": None,
            }
            return _json_response(
                request,
                {
                    "entities": created,
                    "background_jobs": {
                        "memory_projection": {
                            "status": "queued",
                            "job_ids": [projection_job_id],
                            "queued_sources": len(created),
                            "skipped_sources": 0,
                        },
                        "memory_extraction": {
                            "status": "queued",
                            "job_ids": [extraction_job_id],
                            "queued_sources": len(created),
                            "skipped_sources": 0,
                            "queue_depth": EXPECTED_EXTRACTION_QUEUE_DEPTH,
                        },
                    },
                },
                status_code=201,
            )
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            return _json_response(request, state["jobs"][job_id])
        if path == "/api/search":
            payload = json.loads(request.content)
            query = payload["query"]
            entities = list(state["entities"])
            if query != "LongMemEval":
                _assert_question_search_payload(module, payload)
                entities.sort(
                    key=lambda entity: entity["metadata"]["longmemeval_session_id"] != "s2"
                )
            results = [
                {
                    "id": entity["id"],
                    "type": "session",
                    "name": "fixture",
                    "content": "",
                    "score": 1.0 - (index * 0.1),
                    "result_origin": "graph",
                    "metadata": entity["metadata"],
                }
                for index, entity in enumerate(entities)
            ]
            return _json_response(request, {"results": results, "total": len(results)})
        return _native_context_response(request, module.DEFAULT_QA_CONTEXT_TOKENS)

    report = asyncio.run(
        module.run_benchmark(
            data_path,
            api_url="http://ci-sibyl/api",
            limit=1,
            concurrency=1,
            k_values=[1, 2, 5, 10],
            command=["longmemeval_live.py", "fixture.json"],
            verify_sha256=False,
            wait_for_memory_projection=True,
            memory_projection_timeout_seconds=1,
            wait_for_memory_extraction=True,
            memory_extraction_timeout_seconds=1,
            qa_mode="fixture",
            qa_context_arm=qa_context_arm,
            transport=httpx.MockTransport(handler),
        )
    )

    _assert_gate_valid_report(module, report)
    if qa_context_arm == "historical-prefix-v1":
        _assert_fixture_qa_report(report)
    else:
        qa = report["case_results"][0]["qa"]
        assert qa["context_receipt"]["rendered_context"] == qa["native_context"]["markdown"]
        assert "native-only fact" in qa["context_receipt"]["reader_prompt"]
        assert "I bought markers" not in qa["context_receipt"]["reader_prompt"]
        assert qa["context_session_ids"] == []
        assert report["runtime"]["qa_retrieval_surface"] == "POST /api/context/pack"
    _assert_memory_extraction_stats(report)
    _assert_memory_projection_stats(report)
    _assert_chunked_entities(module, state)


def test_longmemeval_live_stall_timeout_reports_active_case(tmp_path: Path) -> None:
    module = _load_live_module()
    data_path = tmp_path / "longmemeval_s_cleaned.json"
    data_path.write_text(
        json.dumps(
            [
                {
                    "question_id": "q1",
                    "question_type": "single-session-user",
                    "question": "What did I buy?",
                    "question_date": "2026/01/03 12:00",
                    "answer_session_ids": ["s1"],
                    "haystack_session_ids": ["s1"],
                    "haystack_dates": ["2026/01/02"],
                    "haystack_sessions": [[{"role": "user", "content": "I bought markers."}]],
                }
            ]
        ),
        encoding="utf-8",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return _json_response(request, {"status": "ok"})
        if path == "/api/auth/local/signup":
            return _json_response(
                request,
                {"access_token": "fixture-token", "organization": {"id": "org", "slug": "org"}},
                status_code=201,
            )
        if path == "/api/entities/bulk":
            await asyncio.sleep(1.0)
        return _json_response(request, {"results": []})

    with pytest.raises(module.LongMemEvalLiveError, match=r"active=\[case=0") as exc_info:
        asyncio.run(
            module.run_benchmark(
                data_path,
                api_url="http://ci-sibyl/api",
                limit=1,
                concurrency=1,
                command=["longmemeval_live.py", "fixture.json"],
                heartbeat_interval_seconds=0.01,
                stall_timeout_seconds=0.01,
                verify_sha256=False,
                transport=httpx.MockTransport(handler),
            )
        )
    message = str(exc_info.value)
    assert "phase=ingest" in message
    assert "doc=1/1" in message
    assert "path=/entities" in message


def test_longmemeval_live_stratified_selection_and_diagnostics(tmp_path: Path) -> None:
    module = _load_live_module()
    data_path = tmp_path / "longmemeval_s_cleaned.json"
    data_path.write_text(
        json.dumps(
            [
                {
                    "question_id": "q-user-1",
                    "question_type": "single-session-user",
                    "question": "What did I buy?",
                    "question_date": "2026/01/03 12:00",
                    "answer_session_ids": ["s-user-answer"],
                    "haystack_session_ids": ["s-user-answer"],
                    "haystack_dates": ["2026/01/02"],
                    "haystack_sessions": [[{"role": "user", "content": "I bought markers."}]],
                },
                {
                    "question_id": "q-user-2",
                    "question_type": "single-session-user",
                    "question": "What did I bring?",
                    "question_date": "2026/01/03 12:00",
                    "answer_session_ids": ["s-user-second"],
                    "haystack_session_ids": ["s-user-second"],
                    "haystack_dates": ["2026/01/02"],
                    "haystack_sessions": [[{"role": "user", "content": "I brought tea."}]],
                },
                {
                    "question_id": "q-pref",
                    "question_type": "single-session-preference",
                    "question": "What snack should I serve?",
                    "question_date": "2026/01/03 12:00",
                    "answer_session_ids": ["s-pref-answer"],
                    "haystack_session_ids": ["s-pref-answer", "s-pref-distractor"],
                    "haystack_dates": ["2026/01/02", "2026/01/01"],
                    "haystack_sessions": [
                        [{"role": "user", "content": "I love salty snacks."}],
                        [{"role": "user", "content": "I like sweet desserts."}],
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )
    state: dict[str, Any] = {"entities": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return _json_response(request, {"status": "ok"})
        if path == "/api/auth/local/signup":
            return _json_response(
                request,
                {"access_token": "fixture-token", "organization": {"id": "org", "slug": "org"}},
                status_code=201,
            )
        if path == "/api/entities/bulk":
            payload = json.loads(request.content)
            created = _bulk_create_fixture_entities(state, payload)
            return _json_response(request, {"entities": created}, status_code=201)
        if path == "/api/search":
            payload = json.loads(request.content)
            entities = list(state["entities"])
            if payload["query"] != "LongMemEval":
                if "snack" in payload["query"]:
                    entities = [
                        entity
                        for entity in entities
                        if entity["metadata"]["longmemeval_case_index"] == PREFERENCE_CASE_INDEX
                    ]
                else:
                    entities = [
                        entity
                        for entity in entities
                        if entity["metadata"]["longmemeval_case_index"] == 0
                    ]
                entities.sort(
                    key=lambda entity: entity["metadata"]["longmemeval_session_id"].endswith(
                        "answer"
                    )
                )
            results = [
                {
                    "id": entity["id"],
                    "type": "session",
                    "score": 1.0,
                    "result_origin": "graph",
                    "metadata": entity["metadata"],
                }
                for entity in entities
            ]
            results = results[: int(payload.get("limit", len(results)))]
            return _json_response(request, {"results": results, "total": len(results)})
        return _json_response(request, {"detail": "not found"}, status_code=404)

    report = asyncio.run(
        module.run_benchmark(
            data_path,
            api_url="http://ci-sibyl/api",
            limit=2,
            concurrency=1,
            k_values=[1],
            sample_strategy="stratified",
            command=["longmemeval_live.py", "fixture.json"],
            verify_sha256=False,
            transport=httpx.MockTransport(handler),
        )
    )

    assert report["dataset"]["selected_case_indices"] == [2, 0]
    assert report["case_results"][0]["case_index"] == 0
    assert report["case_results"][0]["missed_answer_session_ids"] == []
    assert report["case_results"][1]["case_index"] == PREFERENCE_CASE_INDEX
    assert report["case_results"][1]["answer_ranks"] == [{"session_id": "s-pref-answer", "rank": 2}]
    assert report["case_results"][1]["missed_answer_session_ids"] == []
    worst = report["diagnostics"]["worst_cases"][0]
    assert worst["case_index"] == PREFERENCE_CASE_INDEX
    assert "salty snacks" in worst["answer_snippets"]["s-pref-answer"]
    assert "sweet desserts" in worst["top_distractor_snippets"]["s-pref-distractor"]


def test_qa_historical_prompt_stays_byte_identical() -> None:
    _load_live_module()
    qa = importlib.import_module("longmemeval_qa")

    entry = {
        "question": "What degree did I graduate with?",
        "question_date": "2026/01/03",
        "answer": "Business Administration",
        "answer_session_ids": ["s1"],
        "haystack_session_ids": ["s1"],
        "haystack_dates": ["2025/12/01"],
        "haystack_sessions": [[{"role": "user", "content": "x" * 5000}]],
    }
    result = asyncio.run(
        qa.evaluate_longmemeval_case_qa(
            entry,
            ranked_session_ids=["s1"],
            corpus_text_policy="user-and-assistant-turns-v1",
            config=qa.LongMemEvalQAConfig(mode="fixture"),
        )
    )
    text = ("User: " + "x" * 5000)[:3985].rstrip() + " [truncated]"
    assert result["context_receipt"]["reader_prompt"] == (
        "Question date: 2026/01/03\nQuestion: What degree did I graduate with?\n\n"
        f"Retrieved sessions:\nRank 1 session s1:\n{text}\n\nAnswer:"
    )
    assert result["max_context_tokens"] is None


def test_qa_query_passages_preserve_tail_evidence_dates_and_budget() -> None:
    _load_live_module()
    qa = importlib.import_module("longmemeval_qa")

    content = "User: An unrelated observation about the weather.\n" * 140
    content += "User: I graduated with a degree in Business Administration.\n"
    document = LongMemEvalCorpusDocument("s1", content, "2025/12/01")
    config = qa.LongMemEvalQAConfig(context_arm="query-passages-v1", max_context_tokens=150)
    _, rendered, spans = qa.render_qa_context(
        question="What degree did I graduate with?",
        documents=[document],
        ranked_session_ids=["s1"],
        config=config,
    )
    assert "Business Administration" in rendered
    assert "Date: 2025/12/01" in rendered
    assert qa.count_context_tokens(rendered) <= config.max_context_tokens
    assert spans[0]["end"] == len(content)
    assert content[spans[0]["start"] : spans[0]["end"]] in rendered


@pytest.mark.parametrize("arm", ["dated-prefix-v1", "query-passages-v1"])
def test_qa_selection_cannot_observe_answer_labels(arm: str) -> None:
    _load_live_module()
    qa = importlib.import_module("longmemeval_qa")

    entry = {
        "question": "Which version is current?",
        "question_date": "2026/01/03",
        "answer": "first answer",
        "answer_session_ids": ["s1"],
        "haystack_session_ids": ["s1"],
        "haystack_dates": ["2026/01/01"],
        "haystack_sessions": [
            [{"role": "user", "content": "Version 3 is current.", "has_answer": True}]
        ],
    }
    config = qa.LongMemEvalQAConfig(mode="fixture", context_arm=arm)
    before = asyncio.run(
        qa.evaluate_longmemeval_case_qa(
            entry,
            ranked_session_ids=["s1"],
            corpus_text_policy="user-and-assistant-turns-v1",
            config=config,
        )
    )
    entry["answer"] = "POISONED_GOLD"
    entry["answer_session_ids"] = ["absent"]
    entry["haystack_sessions"][0][0]["has_answer"] = False
    after = asyncio.run(
        qa.evaluate_longmemeval_case_qa(
            entry,
            ranked_session_ids=["s1"],
            corpus_text_policy="user-and-assistant-turns-v1",
            config=config,
        )
    )
    assert before["context_receipt"] == after["context_receipt"]


def test_qa_complete_controls_fail_instead_of_truncating() -> None:
    _load_live_module()
    qa = importlib.import_module("longmemeval_qa")

    text = "A complete sentence with evidence.\n" * 100
    for arm in ("full-sessions-v1", "native-context-v1"):
        with pytest.raises(ValueError, match=r"exceeds? QA token ceiling"):
            qa.render_qa_context(
                question="Evidence?",
                documents=[LongMemEvalCorpusDocument("s1", text)],
                ranked_session_ids=["s1"],
                native_markdown=text,
                config=qa.LongMemEvalQAConfig(context_arm=arm, max_context_tokens=10),
            )
    _, rendered, spans = qa.render_qa_context(
        question="Evidence?",
        documents=[],
        ranked_session_ids=[],
        native_markdown=text,
        config=qa.LongMemEvalQAConfig(context_arm="native-context-v1"),
    )
    assert rendered == text
    assert spans == []


@pytest.mark.parametrize(
    ("variant", "expected_success"),
    [
        ("missing", False),
        ("bootstrap", True),
        ("partial", False),
        ("matching", True),
        ("wrong-arm", False),
        ("wrong-budget", False),
        ("wrong-model", False),
        ("wrong-rubric", False),
        ("wrong-corpus", False),
        ("wrong-policy", False),
        ("incomplete", False),
        ("missing-accuracy", False),
        ("null-accuracy", False),
        ("string-accuracy", False),
        ("nan-accuracy", False),
        ("infinite-accuracy", False),
        ("negative-accuracy", False),
        ("excess-accuracy", False),
        ("missing-correct-count", False),
        ("fractional-correct-count", False),
        ("excess-correct-count", False),
        ("incoherent-accuracy", False),
    ],
)
def test_eval_workflow_qa_guard_fails_closed(
    tmp_path: Path,
    variant: str,
    expected_success: bool,
) -> None:
    workflow = yaml.safe_load(
        (Path(__file__).parents[2] / ".github/workflows/eval.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["longmemeval-live-full"]["steps"]
        if step.get("name") == "Guard QA comparison contract before spending"
    )
    baseline = tmp_path / "benchmarks/results/ai-memory/pinned-longmemeval-s-qa.json"
    payload: dict[str, Any] = {
        "qa": {
            "mode": "model",
            "context_arm": "query-passages-v1",
            "reader_provider": "openai",
            "judge_provider": "openai",
            "reader_model": "reader",
            "judge_model": "judge",
            "reader_prompt_id": "sibyl-longmemeval-reader-v1",
            "judge_prompt_id": "sibyl-longmemeval-judge-v1",
            "rubric_id": "longmemeval-s-answer-correctness-v1",
            "context_tokenizer": "o200k_base",
            "max_context_tokens": 6000,
            "max_context_sessions": 5,
            "max_session_chars": 4000,
        },
        "dataset": {"corpus_hash": "sha256:fixture", "corpus_text_policy": "fixture-policy"},
        "completion_status": "complete",
        "total_questions": 500,
        "overall": {"qa_evaluated_count": 500, "qa_correct_count": 131, "qa_accuracy": 0.262},
    }
    mutations = {
        "wrong-arm": ("qa", "context_arm", "native-context-v1"),
        "wrong-budget": ("qa", "max_context_tokens", 8000),
        "wrong-model": ("qa", "reader_model", "different-reader"),
        "wrong-rubric": ("qa", "rubric_id", "different-rubric"),
        "wrong-corpus": ("dataset", "corpus_hash", "sha256:different"),
        "wrong-policy": ("dataset", "corpus_text_policy", "different-policy"),
        "incomplete": ("overall", "qa_evaluated_count", 499),
        "null-accuracy": ("overall", "qa_accuracy", None),
        "string-accuracy": ("overall", "qa_accuracy", "0.262"),
        "nan-accuracy": ("overall", "qa_accuracy", float("nan")),
        "infinite-accuracy": ("overall", "qa_accuracy", float("inf")),
        "negative-accuracy": ("overall", "qa_accuracy", -0.1),
        "excess-accuracy": ("overall", "qa_accuracy", 1.1),
        "fractional-correct-count": ("overall", "qa_correct_count", 131.5),
        "excess-correct-count": ("overall", "qa_correct_count", 501),
        "incoherent-accuracy": ("overall", "qa_accuracy", 0.8),
    }
    if variant in mutations:
        section, key, value = mutations[variant]
        payload[section][key] = value
    if variant == "missing-accuracy":
        del payload["overall"]["qa_accuracy"]
    if variant == "missing-correct-count":
        del payload["overall"]["qa_correct_count"]
    if variant not in {"missing", "bootstrap"}:
        baseline.parent.mkdir(parents=True)
        baseline.write_text(json.dumps(payload))
    bash = which("bash")
    assert bash is not None
    assert which("jq") is not None
    result = subprocess.run(  # noqa: S603
        [bash, "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "LONGMEMEVAL_QA_BOOTSTRAP": str(variant == "bootstrap").lower(),
            "LONGMEMEVAL_QA_LIMIT": "25" if variant == "partial" else "",
            "LONGMEMEVAL_QA_CONTEXT_ARM": "query-passages-v1",
            "LONGMEMEVAL_QA_CONTEXT_TOKENS": "6000",
            "LONGMEMEVAL_QA_READER_MODEL": "reader",
            "LONGMEMEVAL_QA_JUDGE_MODEL": "judge",
            "LONGMEMEVAL_SHA256": "fixture",
            "LONGMEMEVAL_CORPUS_TEXT_POLICY": "fixture-policy",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) == expected_success, result.stdout + result.stderr
    if variant == "bootstrap":
        assert "unclaimed candidate evidence" in result.stdout
        assert not baseline.exists()


def test_qa_passage_keeps_speaker_after_first_sentence() -> None:
    _load_live_module()
    qa = importlib.import_module("longmemeval_qa")
    text = "User: Hello. I graduated in biology.\nAssistant: Great. Biology is fascinating."
    _, rendered, spans = qa.render_qa_context(
        question="What did I graduate in?",
        documents=[LongMemEvalCorpusDocument("s1", text)],
        ranked_session_ids=["s1"],
        config=qa.LongMemEvalQAConfig(context_arm="query-passages-v1"),
    )
    assert spans[0]["speaker"] == "User"
    assert "Speaker: User" in rendered
    assert any(span["speaker"] == "Assistant" for span in spans)


@pytest.mark.parametrize(("budget", "sessions"), [(99, 5), (32_001, 5), (6000, 0), (6000, 51)])
def test_qa_native_invalid_request_fails_before_any_api_call(
    tmp_path: Path,
    budget: int,
    sessions: int,
) -> None:
    module = _load_live_module()
    dataset = tmp_path / "fixture.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "question_id": "q1",
                    "question": "What did I buy?",
                    "question_type": "single-session-user",
                    "answer_session_ids": ["s1"],
                    "haystack_session_ids": ["s1"],
                    "haystack_sessions": [[{"role": "user", "content": "I bought pencils."}]],
                }
            ]
        )
    )

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Invalid native config reached API: {request.url.path}")

    with pytest.raises(module.LongMemEvalLiveError, match="Native QA"):
        asyncio.run(
            module.run_benchmark(
                dataset,
                api_url="http://fixture/api",
                verify_sha256=False,
                qa_mode="fixture",
                qa_context_arm="native-context-v1",
                qa_max_context_tokens=budget,
                qa_max_context_sessions=sessions,
                transport=httpx.MockTransport(unexpected_request),
            )
        )


@pytest.mark.parametrize(("budget", "sessions"), [(100, 1), (32_000, 50)])
def test_qa_native_request_accepts_product_boundary_values(budget: int, sessions: int) -> None:
    module = _load_live_module()
    config = module._qa_config(
        mode="fixture",
        reader_provider="openai",
        reader_model="reader",
        judge_provider="openai",
        judge_model="judge",
        max_context_sessions=sessions,
        max_session_chars=4000,
        timeout_seconds=120,
        context_arm="native-context-v1",
        max_context_tokens=budget,
    )
    assert config.max_context_tokens == budget
    assert config.max_context_sessions == sessions
