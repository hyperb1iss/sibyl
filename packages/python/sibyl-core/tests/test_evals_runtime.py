from __future__ import annotations

from unittest.mock import patch

import pytest

from sibyl_core.evals import (
    ContextPackCaseResult,
    ContextPackEvalCase,
    ContextPackEvalReport,
    ContextPackEvalResult,
    ContextPackFixture,
    EvalConfig,
    EvalQuery,
    EvalRunner,
)


class _MockResponse:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _MockClient:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(self, endpoint: str, json: dict[str, object]) -> _MockResponse:
        self.calls.append((endpoint, json))
        return _MockResponse(self.payload)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_query_parses_unified_search_results() -> None:
    runner = EvalRunner(EvalConfig(save_results=False))
    runner._http_client = _MockClient(
        {
            "results": [
                {
                    "id": "doc-1",
                    "type": "document",
                    "content": "Install FastAPI with uv.",
                    "score": 0.92,
                    "result_origin": "document",
                }
            ]
        }
    )

    result = await runner.run_query(
        EvalQuery(query="install fastapi", expected_ids=["doc-1"]),
        search_type="unified",
    )

    assert result.error is None
    assert result.results[0].id == "doc-1"
    assert result.results[0].metadata["result_origin"] == "document"
    assert result.metrics.mrr == 1.0
    assert runner._http_client.calls == [
        ("/search", {"query": "install fastapi", "limit": 20, "include_content": True})
    ]


@pytest.mark.asyncio
async def test_run_query_parses_code_examples_results() -> None:
    runner = EvalRunner(EvalConfig(save_results=False))
    runner._http_client = _MockClient(
        {
            "examples": [
                {
                    "chunk_id": "chunk-1",
                    "code": "print('hi')",
                    "similarity": 0.81,
                    "language": "python",
                }
            ]
        }
    )

    result = await runner.run_query(
        EvalQuery(query="python print", expected_ids=["chunk-1"]),
        search_type="code-examples",
    )

    assert result.error is None
    assert result.results[0].id == "chunk-1"
    assert result.results[0].metadata["language"] == "python"
    assert runner._http_client.calls == [
        ("/rag/code-examples", {"query": "python print", "match_count": 20})
    ]


@pytest.mark.asyncio
async def test_run_query_captures_request_errors() -> None:
    class _FailingClient:
        async def post(self, endpoint: str, json: dict[str, object]) -> _MockResponse:
            raise RuntimeError("boom")

        async def aclose(self) -> None:
            return None

    runner = EvalRunner(EvalConfig(save_results=False))
    runner._http_client = _FailingClient()

    result = await runner.run_query(
        EvalQuery(query="missing", expected_ids=["doc-1"]),
        search_type="unified",
    )

    assert result.error == "boom"
    assert result.results == []
    assert result.metrics.mrr == 0.0


@pytest.mark.asyncio
async def test_run_context_pack_case_posts_pack_request() -> None:
    runner = EvalRunner(EvalConfig(save_results=False))
    runner._http_client = _MockClient(
        {
            "goal": "handoff native memory implementation",
            "intent": "build",
            "query": "handoff native memory implementation sibyl",
            "domain": "sibyl",
            "project": "project-sibyl",
            "usage_hint": "use the pack",
            "total_items": 1,
            "sections": [
                {
                    "facet": "decisions",
                    "title": "Decisions",
                    "items": [
                        {
                            "id": "decision-source-law",
                            "type": "decision",
                            "name": "Raw memory is source law",
                            "content": "Preserve source IDs before extraction.",
                            "score": 0.9,
                            "facet": "decisions",
                            "reason": "decision records a choice",
                            "source": "northstar",
                            "metadata": {"source_id": "northstar"},
                        }
                    ],
                }
            ],
        }
    )

    result = await runner.run_context_pack_case(
        ContextPackEvalCase(
            name="coding-handoff",
            goal="handoff native memory implementation",
            domain="sibyl",
            project="project-sibyl",
            agent_id="nova",
            limit=8,
            include_related=False,
            fixture=ContextPackFixture(
                name="coding-handoff",
                required_item_ids={"decision-source-law"},
                require_source_metadata=True,
            ),
        )
    )

    assert result.error is None
    assert result.result.passed
    assert runner._http_client.calls == [
        (
            "/context/pack",
            {
                "goal": "handoff native memory implementation",
                "intent": "build",
                "layer": "recall",
                "limit": 8,
                "include_related": False,
                "related_limit": 3,
                "domain": "sibyl",
                "project": "project-sibyl",
                "agent_id": "nova",
            },
        )
    ]


@pytest.mark.asyncio
async def test_run_context_pack_case_enforces_latency_budget() -> None:
    runner = EvalRunner(EvalConfig(save_results=False))
    runner._http_client = _MockClient(
        {
            "goal": "ship faster",
            "intent": "build",
            "query": "ship faster",
            "total_items": 0,
            "sections": [],
        }
    )

    with patch("sibyl_core.evals.runtime.time.perf_counter", side_effect=[10.0, 10.35]):
        result = await runner.run_context_pack_case(
            ContextPackEvalCase(
                name="latency-budget",
                goal="ship faster",
                fixture=ContextPackFixture(
                    name="latency-budget",
                    max_latency_ms=250.0,
                ),
            )
        )

    assert result.error is None
    assert not result.result.passed
    assert result.latency_ms == pytest.approx(350.0)
    assert result.result.failures == ["latency too high: 350.0 ms > 250.0 ms"]
    assert result.result.metrics["latency_ms"] == pytest.approx(350.0)


def test_context_pack_report_exposes_leak_and_token_metrics() -> None:
    report = ContextPackEvalReport(
        cases=[
            ContextPackCaseResult(
                case=ContextPackEvalCase(
                    name="private-leak-negative",
                    goal="keep private memory private",
                    fixture=ContextPackFixture(name="private-leak-negative"),
                ),
                result=ContextPackEvalResult(
                    fixture="private-leak-negative",
                    passed=False,
                    metrics={
                        "estimated_tokens": 42,
                        "source_metadata_coverage": 1.0,
                        "facet_order_matches": True,
                        "forbidden_item_matches": 1,
                        "forbidden_term_matches": 0,
                    },
                ),
                latency_ms=123.0,
            )
        ]
    )

    payload = report.to_dict()

    assert payload["token_estimator"] == {
        "method": "approximate_character_count",
        "characters_per_token": 4,
        "safety_margin_multiplier": 1.2,
    }
    assert payload["metrics"]["max_estimated_tokens"] == 42
    assert payload["metrics"]["max_budgeted_estimated_tokens"] == 51
    assert payload["metrics"]["leak_count"] == 1
    assert payload["metrics"]["forbidden_item_matches"] == 1


def test_context_pack_report_does_not_double_count_same_case_leak_signals() -> None:
    report = ContextPackEvalReport(
        cases=[
            ContextPackCaseResult(
                case=ContextPackEvalCase(
                    name="private-leak-negative",
                    goal="keep private memory private",
                    fixture=ContextPackFixture(name="private-leak-negative"),
                ),
                result=ContextPackEvalResult(
                    fixture="private-leak-negative",
                    passed=False,
                    metrics={
                        "forbidden_item_matches": 1,
                        "forbidden_term_matches": 1,
                    },
                ),
            )
        ]
    )

    payload = report.to_dict()

    assert payload["metrics"]["forbidden_item_matches"] == 1
    assert payload["metrics"]["forbidden_term_matches"] == 1
    assert payload["metrics"]["leak_count"] == 1


@pytest.mark.asyncio
async def test_run_context_pack_case_uses_exception_type_for_blank_errors() -> None:
    class _BlankFailingClient:
        async def post(self, endpoint: str, json: dict[str, object]) -> _MockResponse:
            raise TimeoutError

        async def aclose(self) -> None:
            return None

    runner = EvalRunner(EvalConfig(save_results=False))
    runner._http_client = _BlankFailingClient()

    result = await runner.run_context_pack_case(
        ContextPackEvalCase(
            name="context-pack-smoke",
            goal="ship faster",
            fixture=ContextPackFixture(name="context-pack-smoke"),
        )
    )

    assert result.error == "TimeoutError"
    assert result.result.failures == ["TimeoutError"]
