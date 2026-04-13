from __future__ import annotations

import pytest

from sibyl_core.evals import EvalConfig, EvalQuery, EvalRunner


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
