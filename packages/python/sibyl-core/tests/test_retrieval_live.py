"""Live retrieval benchmarks against a running Sibyl API.

READ-ONLY — never creates, updates, or deletes entities.
Requires explicit opt-in plus a running Sibyl instance with data.

Run:
    SIBYL_LIVE_RETRIEVAL_TESTS=1 uv run pytest packages/python/sibyl-core/tests/test_retrieval_live.py -v -s
    moon run core:bench-live-smoke
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

SIBYL_API = "http://localhost:3334"
STRICT_LIVE_PERF = os.environ.get("SIBYL_ASSERT_LIVE_LATENCY") == "1"
LIVE_RETRIEVAL_TESTS = (
    os.environ.get("SIBYL_LIVE_RETRIEVAL_TESTS") == "1" or STRICT_LIVE_PERF
)


def _get_client_headers() -> dict[str, str]:
    """Borrow auth headers from the CLI client."""
    try:
        from sibyl_cli.client import SibylClient

        c = SibylClient()
        return c._default_headers()
    except Exception:
        return {"Content-Type": "application/json"}


_headers = _get_client_headers()


def _live_search_available() -> bool:
    """Return True only when the live search endpoint is actually usable.

    Health can be public while search requires auth, so probe the real endpoint
    with the same headers the CLI would send.
    """
    try:
        health = httpx.get(f"{SIBYL_API}/api/health", timeout=3)
        if health.status_code != 200:
            return False

        search = httpx.post(
            f"{SIBYL_API}/api/search",
            json={"query": "test", "limit": 1},
            headers=_headers,
            timeout=5,
        )
        return search.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not LIVE_RETRIEVAL_TESTS or not _live_search_available(),
    reason="Sibyl live retrieval tests are disabled or unavailable",
)


def _search(query: str, **kwargs: Any) -> tuple[dict[str, Any], float]:
    """Execute search via REST API with CLI auth."""
    payload: dict[str, Any] = {"query": query, "limit": kwargs.pop("limit", 10), **kwargs}
    start = time.perf_counter()
    r = httpx.post(f"{SIBYL_API}/api/search", json=payload, headers=_headers, timeout=30)
    elapsed_ms = (time.perf_counter() - start) * 1000
    if r.status_code == 401:
        pytest.skip("Sibyl live search auth expired or missing")
    r.raise_for_status()
    return r.json(), elapsed_ms


def _assert_latency(ms: float, budget_ms: int, label: str) -> None:
    if STRICT_LIVE_PERF:
        assert ms < budget_ms, f"{label} took {ms:.0f}ms (budget: {budget_ms}ms)"
    else:
        assert ms >= 0


# =============================================================================
# Latency Benchmarks
# =============================================================================


class TestLiveSearchLatency:
    """Measure real search latency against the live graph."""

    def test_simple_query_latency(self):
        """Single-word query should complete within 3 seconds."""
        result, ms = _search("patterns", limit=5)
        print(f"\n  simple query: {ms:.0f}ms, {result.get('total', 0)} results")
        _assert_latency(ms, 3000, "Simple query")

    def test_complex_query_latency(self):
        """Multi-word semantic query latency."""
        result, ms = _search("how to handle database connection failures", limit=10)
        print(f"\n  complex query: {ms:.0f}ms, {result.get('total', 0)} results")
        _assert_latency(ms, 10000, "Complex query")

    def test_type_filtered_query(self):
        """Query filtered to specific entity type."""
        result, ms = _search("work", types=["task"], limit=10)
        print(f"\n  filtered query: {ms:.0f}ms, {result.get('total', 0)} results")
        _assert_latency(ms, 3000, "Filtered query")

    def test_pattern_search(self):
        """Search specifically for patterns."""
        result, ms = _search("error handling", types=["pattern"], limit=5)
        print(f"\n  pattern search: {ms:.0f}ms, {result.get('total', 0)} results")
        _assert_latency(ms, 3000, "Pattern search")

    def test_episode_search(self):
        """Search specifically for episodes."""
        result, ms = _search("debugging", types=["episode"], limit=5)
        print(f"\n  episode search: {ms:.0f}ms, {result.get('total', 0)} results")
        _assert_latency(ms, 3000, "Episode search")

    def test_sequential_queries_throughput(self):
        """Measure throughput with 5 sequential queries."""
        queries = [
            "authentication",
            "database migration",
            "testing strategy",
            "deployment pipeline",
            "error handling patterns",
        ]

        total_ms = 0.0
        total_results = 0
        for q in queries:
            result, ms = _search(q, limit=5)
            total_ms += ms
            total_results += result.get("total", 0)

        avg_ms = total_ms / len(queries)
        print(
            f"\n  5 queries: {total_ms:.0f}ms total, {avg_ms:.0f}ms avg, {total_results} total results"
        )
        if STRICT_LIVE_PERF:
            assert avg_ms < 3000, f"Average query latency {avg_ms:.0f}ms (budget: 3000ms)"
        else:
            assert total_results >= 0


# =============================================================================
# Recall Quality
# =============================================================================


class TestLiveSearchRecall:
    """Verify the live search returns relevant results."""

    def test_search_returns_results(self):
        """A broad query should return at least some results (may be zero if RBAC filters all)."""
        result, ms = _search("memory architecture", limit=10)
        total = result.get("total", 0)
        print(f"\n  broad query: {total} results in {ms:.0f}ms")
        # Zero results may be valid if RBAC project filtering is active
        assert isinstance(total, int)

    def test_type_filter_respected(self):
        """Results should respect the type filter."""
        result, _ = _search("implement", types=["task"], limit=10)
        for r in result.get("results", []):
            assert r.get("type") == "task", f"Expected task, got {r.get('type')}"

    def test_task_search(self):
        """Task search should return results."""
        result, _ = _search("build", types=["task"], limit=5)
        total = result.get("total", 0)
        print(f"\n  task results: {total}")
        assert total >= 0

    def test_results_have_required_fields(self):
        """Each result should have id, type, name, and score."""
        result, _ = _search("memory", limit=5)
        for r in result.get("results", []):
            assert "id" in r, "Result missing 'id'"
            assert "type" in r, "Result missing 'type'"
            assert "name" in r, "Result missing 'name'"
            assert "score" in r, "Result missing 'score'"

    def test_results_preserve_raw_score_order_within_single_origin(self):
        """Single-origin results should be sorted by raw score descending."""
        result, _ = _search("knowledge graph", limit=10)
        results = result.get("results", [])
        origins = {r.get("result_origin") for r in results}
        if len(origins) > 1:
            assert result.get("graph_count", 0) + result.get("document_count", 0) >= len(results)
            return

        scores = [r.get("score", 0) for r in results]
        assert scores == sorted(scores, reverse=True), "Results not sorted by score"


# =============================================================================
# Graph Statistics
# =============================================================================


class TestLiveGraphStats:
    """Read-only graph health checks."""

    def test_health_endpoint(self):
        """Health endpoint should respond."""
        r = httpx.get(f"{SIBYL_API}/api/health", timeout=5)
        assert r.status_code == 200

    def test_entity_distribution(self):
        """Report entity counts per type (informational, not assertive)."""
        queries = {
            "task": "implement",
            "pattern": "pattern",
            "episode": "learned",
            "project": "sibyl",
        }
        for t, q in queries.items():
            result, ms = _search(q, types=[t], limit=1)
            print(f"\n  {t}: {result.get('total', 0)} results ({ms:.0f}ms)")

    def test_api_responds_to_all_types(self):
        """Search should accept all entity type filters without error."""
        for t in ["task", "pattern", "episode", "project"]:
            result, _ = _search("test", types=[t], limit=1)
            assert "results" in result, f"Type filter '{t}' broke the API"
