"""RAG evaluation runner for benchmarking retrieval quality.

Runs evaluation queries against the RAG system and computes metrics.
Supports both live API testing and mock testing.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from tests.evals.metrics import (
    EvalMetrics,
    EvalQuery,
    RetrievalResult,
    aggregate_metrics,
    compute_metrics,
)

log = structlog.get_logger()


@dataclass
class EvalConfig:
    """Configuration for evaluation run."""

    # API settings
    api_base_url: str = "http://localhost:3334/api"

    # Evaluation settings
    k_values: list[int] = field(default_factory=lambda: [1, 3, 5, 10])
    timeout_seconds: float = 30.0

    # Output settings
    output_dir: Path = field(default_factory=lambda: Path("eval_results"))
    save_results: bool = True


@dataclass
class EvalResult:
    """Result from evaluating a single query."""

    query: EvalQuery
    results: list[RetrievalResult]
    metrics: EvalMetrics
    error: str | None = None


@dataclass
class EvalReport:
    """Complete evaluation report."""

    config: EvalConfig
    queries: list[EvalResult]
    aggregated: EvalMetrics
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> dict[str, Any]:
        """Convert report to dict for serialization."""
        return {
            "timestamp": self.timestamp,
            "num_queries": len(self.queries),
            "metrics": self.aggregated.to_dict(),
            "per_query": [
                {
                    "query": q.query.query,
                    "metrics": q.metrics.to_dict(),
                    "error": q.error,
                }
                for q in self.queries
            ],
        }

    def save(self, path: Path | None = None) -> Path:
        """Save report to JSON file.

        Args:
            path: Optional output path (default: output_dir/eval_TIMESTAMP.json)

        Returns:
            Path to saved file
        """
        if path is None:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            filename = f"eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
            path = self.config.output_dir / filename

        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

        log.info("Saved evaluation report", path=str(path))
        return path

    def print_summary(self) -> None:
        """Print summary to console."""


class EvalRunner:
    """Runner for RAG evaluation."""

    def __init__(self, config: EvalConfig | None = None):
        """Initialize the evaluation runner.

        Args:
            config: Evaluation configuration
        """
        self.config = config or EvalConfig()
        self._http_client = None

    async def _get_client(self):
        """Lazily initialize HTTP client."""
        if self._http_client is None:
            import httpx

            self._http_client = httpx.AsyncClient(
                base_url=self.config.api_base_url,
                timeout=self.config.timeout_seconds,
            )
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def run_query(
        self,
        query: EvalQuery,
        search_type: str = "rag",
    ) -> EvalResult:
        """Run a single evaluation query.

        Args:
            query: Query to evaluate
            search_type: Type of search (rag, hybrid, code-examples)

        Returns:
            EvalResult with metrics
        """
        client = await self._get_client()

        start_time = time.time()
        error = None
        results: list[RetrievalResult] = []

        try:
            # Choose endpoint based on search type
            if search_type == "code-examples":
                endpoint = "/rag/code-examples"
                payload = {"query": query.query, "match_count": 20}
            elif search_type == "hybrid":
                endpoint = "/rag/hybrid-search"
                payload = {"query": query.query, "match_count": 20}
            else:
                endpoint = "/rag/search"
                payload = {"query": query.query, "match_count": 20}

            response = await client.post(endpoint, json=payload)
            response.raise_for_status()

            data = response.json()

            # Parse results
            if search_type == "code-examples":
                for item in data.get("examples", []):
                    results.append(
                        RetrievalResult(
                            id=item.get("chunk_id", ""),
                            content=item.get("code", ""),
                            score=item.get("similarity", 0.0),
                        )
                    )
            else:
                for item in data.get("results", []):
                    results.append(
                        RetrievalResult(
                            id=item.get("chunk_id", item.get("document_id", "")),
                            content=item.get("content", ""),
                            score=item.get("similarity", 0.0),
                        )
                    )

        except Exception as e:
            error = str(e)
            log.exception("Query failed", query=query.query, error=error)

        latency_ms = (time.time() - start_time) * 1000

        # Compute metrics
        metrics = compute_metrics(
            results=results,
            query=query,
            latency_ms=latency_ms,
            k_values=self.config.k_values,
        )

        return EvalResult(
            query=query,
            results=results,
            metrics=metrics,
            error=error,
        )

    async def run_evaluation(
        self,
        queries: list[EvalQuery],
        search_type: str = "rag",
    ) -> EvalReport:
        """Run full evaluation across all queries.

        Args:
            queries: List of evaluation queries
            search_type: Type of search to use

        Returns:
            Complete EvalReport with aggregated metrics
        """
        log.info("Starting evaluation", num_queries=len(queries), search_type=search_type)

        results = []
        for i, query in enumerate(queries):
            log.debug("Running query", index=i + 1, total=len(queries), query=query.query[:50])
            result = await self.run_query(query, search_type)
            results.append(result)

        # Aggregate metrics
        all_metrics = [r.metrics for r in results]
        aggregated = aggregate_metrics(all_metrics)

        report = EvalReport(
            config=self.config,
            queries=results,
            aggregated=aggregated,
        )

        if self.config.save_results:
            report.save()

        log.info(
            "Evaluation complete",
            num_queries=len(queries),
            ndcg_at_10=aggregated.ndcg_at_k.get(10, 0),
            success_at_5=aggregated.success_at_k.get(5, 0),
        )

        return report

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args):
        """Async context manager exit."""
        await self.close()


# =============================================================================
# Sample Evaluation Queries
# =============================================================================


def get_sample_queries() -> list[EvalQuery]:
    """Get sample evaluation queries for testing.

    These queries test different retrieval scenarios:
    - Simple keyword queries
    - Complex multi-hop queries
    - Code-specific queries
    - Negative queries (should return nothing)
    """
    return [
        EvalQuery(
            query="How to install FastAPI",
            expected_ids=["fastapi-install-1", "fastapi-quickstart-1"],
            relevance_grades={
                "fastapi-install-1": 3,
                "fastapi-quickstart-1": 2,
            },
        ),
        EvalQuery(
            query="authentication best practices",
            expected_ids=["auth-patterns-1", "security-guide-1", "jwt-setup-1"],
            relevance_grades={
                "auth-patterns-1": 3,
                "security-guide-1": 2,
                "jwt-setup-1": 2,
            },
        ),
        EvalQuery(
            query="database connection pooling Python",
            expected_ids=["sqlalchemy-pool-1", "async-db-1"],
            relevance_grades={
                "sqlalchemy-pool-1": 3,
                "async-db-1": 2,
            },
        ),
        EvalQuery(
            query="error handling patterns async await",
            expected_ids=["async-errors-1", "exception-patterns-1"],
            relevance_grades={
                "async-errors-1": 3,
                "exception-patterns-1": 2,
            },
        ),
        EvalQuery(
            query="GraphQL subscription implementation",
            expected_ids=["graphql-subscriptions-1"],
            relevance_grades={
                "graphql-subscriptions-1": 3,
            },
        ),
    ]


# =============================================================================
# CLI Entry Point
# =============================================================================


async def run_evaluation_cli(
    queries_file: Path | None = None,
    search_type: str = "rag",
    api_url: str = "http://localhost:3334/api",
) -> None:
    """CLI entry point for running evaluation.

    Args:
        queries_file: Optional JSON file with queries
        search_type: Type of search to evaluate
        api_url: Base API URL
    """
    # Load queries
    if queries_file and queries_file.exists():
        with open(queries_file) as f:  # noqa: ASYNC230
            data = json.load(f)
            queries = [
                EvalQuery(
                    query=q["query"],
                    expected_ids=q.get("expected_ids", []),
                    relevance_grades=q.get("relevance_grades", {}),
                )
                for q in data["queries"]
            ]
    else:
        queries = get_sample_queries()

    config = EvalConfig(api_base_url=api_url)

    async with EvalRunner(config) as runner:
        report = await runner.run_evaluation(queries, search_type)
        report.print_summary()


if __name__ == "__main__":
    import sys

    queries_file = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(run_evaluation_cli(queries_file))
