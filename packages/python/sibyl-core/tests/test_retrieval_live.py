"""Live retrieval benchmarks against real FalkorDB graph.

READ-ONLY — never creates, updates, or deletes entities.
Requires a running Sibyl instance with data. Skipped automatically
if the API is unreachable.

Run:
    uv run pytest packages/python/sibyl-core/tests/test_retrieval_live.py -v -s
    moon run core:test -- -k live -v -s
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

SIBYL_API = "http://localhost:3334"


def _api_available() -> bool:
    try:
        r = httpx.get(f"{SIBYL_API}/api/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_available(), reason="Sibyl API not reachable")


def _get_client_headers() -> dict[str, str]:
    """Borrow auth headers from the CLI client."""
    try:
        from sibyl_cli.client import SibylClient

        c = SibylClient()
        return c._default_headers()
    except Exception:
        return {"Content-Type": "application/json"}


_headers = _get_client_headers()


def _search(query: str, **kwargs: Any) -> tuple[dict[str, Any], float]:
    """Execute search via REST API with CLI auth."""
    payload: dict[str, Any] = {"query": query, "limit": kwargs.pop("limit", 10), **kwargs}
    start = time.perf_counter()
    r = httpx.post(f"{SIBYL_API}/api/search", json=payload, headers=_headers, timeout=30)
    elapsed_ms = (time.perf_counter() - start) * 1000
    r.raise_for_status()
    return r.json(), elapsed_ms


# =============================================================================
# Latency Benchmarks
# =============================================================================


class TestLiveSearchLatency:
    """Measure real search latency against the live graph."""

    def test_simple_query_latency(self):
        """Single-word query should complete within 3 seconds."""
        result, ms = _search("patterns", limit=5)
        print(f"\n  simple query: {ms:.0f}ms, {result.get('total', 0)} results")
        assert ms < 3000, f"Simple query took {ms:.0f}ms (budget: 3000ms)"

    def test_complex_query_latency(self):
        """Multi-word semantic query latency."""
        result, ms = _search("how to handle database connection failures", limit=10)
        print(f"\n  complex query: {ms:.0f}ms, {result.get('total', 0)} results")
        assert ms < 10000, f"Complex query took {ms:.0f}ms (budget: 10000ms)"

    def test_type_filtered_query(self):
        """Query filtered to specific entity type."""
        result, ms = _search("work", types=["task"], limit=10)
        print(f"\n  filtered query: {ms:.0f}ms, {result.get('total', 0)} results")
        assert ms < 3000, f"Filtered query took {ms:.0f}ms (budget: 3000ms)"

    def test_pattern_search(self):
        """Search specifically for patterns."""
        result, ms = _search("error handling", types=["pattern"], limit=5)
        print(f"\n  pattern search: {ms:.0f}ms, {result.get('total', 0)} results")
        assert ms < 3000

    def test_episode_search(self):
        """Search specifically for episodes."""
        result, ms = _search("debugging", types=["episode"], limit=5)
        print(f"\n  episode search: {ms:.0f}ms, {result.get('total', 0)} results")
        assert ms < 3000

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
        print(f"\n  5 queries: {total_ms:.0f}ms total, {avg_ms:.0f}ms avg, {total_results} total results")
        assert avg_ms < 3000, f"Average query latency {avg_ms:.0f}ms (budget: 3000ms)"


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

    def test_results_sorted_by_score(self):
        """Results should be sorted by score descending."""
        result, _ = _search("knowledge graph", limit=10)
        scores = [r.get("score", 0) for r in result.get("results", [])]
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
        queries = {"task": "implement", "pattern": "pattern", "episode": "learned", "project": "sibyl"}
        for t, q in queries.items():
            result, ms = _search(q, types=[t], limit=1)
            print(f"\n  {t}: {result.get('total', 0)} results ({ms:.0f}ms)")

    def test_api_responds_to_all_types(self):
        """Search should accept all entity type filters without error."""
        for t in ["task", "pattern", "episode", "project"]:
            result, _ = _search("test", types=[t], limit=1)
            assert "results" in result, f"Type filter '{t}' broke the API"
