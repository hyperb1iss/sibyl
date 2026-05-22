from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

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
        "  longmemeval-live-full:",
        1,
    )[0]
    full_job = workflow.split("  longmemeval-live-full:", 1)[1]

    assert "Enable queued LLM memory extraction during LongMemEval smoke only" in workflow
    assert "SIBYL_AUTO_EXTRACT_ENTITIES: ${{ inputs.longmemeval_auto_extract_entities }}" in (
        smoke_job
    )
    assert 'SIBYL_AUTO_EXTRACT_ENTITIES: "false"' in full_job
    assert "SIBYL_LLM_MEMORY_PROVIDER" not in full_job
    assert "--wait-for-memory-extraction" not in full_job
    assert "Full LongMemEval must run with SIBYL_AUTO_EXTRACT_ENTITIES=false." in full_job
    assert '--metadata auto_extract_entities="${SIBYL_AUTO_EXTRACT_ENTITIES}"' in full_job
    assert 'SIBYL_LOCAL_AUTH_ENABLED: "true"' in smoke_job
    assert 'SIBYL_LOCAL_AUTH_ENABLED: "true"' in full_job


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
    assert report["case_results"][0]["ranked_session_ids"] == ["s2", "s1"]
    assert report["case_results"][0]["answer_ranks"] == [{"session_id": "s2", "rank": 1}]
    assert report["case_results"][0]["missed_answer_session_ids"] == []
    assert report["case_results"][0]["created_entity_count"] == EXPECTED_CREATED_ENTITIES
    assert report["case_results"][0]["chunked_session_count"] == 1
    assert report["diagnostics"]["case_gap_count"] == 0


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


def test_longmemeval_live_builds_gate_valid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        return _json_response(request, {"detail": "not found"}, status_code=404)

    report = asyncio.run(
        module.run_benchmark(
            data_path,
            api_url="http://ci-sibyl/api",
            limit=1,
            concurrency=1,
            k_values=[1, 2],
            command=["longmemeval_live.py", "fixture.json"],
            verify_sha256=False,
            wait_for_memory_projection=True,
            memory_projection_timeout_seconds=1,
            wait_for_memory_extraction=True,
            memory_extraction_timeout_seconds=1,
            transport=httpx.MockTransport(handler),
        )
    )

    _assert_gate_valid_report(module, report)
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
