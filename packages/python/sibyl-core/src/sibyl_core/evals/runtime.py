"""Runtime evaluation harnesses for live Sibyl search surfaces."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog

from sibyl_core.evals.metrics import (
    EvalMetrics,
    EvalQuery,
    RetrievalResult,
    aggregate_metrics,
    compute_metrics,
)

log = structlog.get_logger()
SearchType = Literal["unified", "rag", "hybrid", "code-examples"]


@dataclass
class EvalConfig:
    """Configuration for an evaluation run."""

    api_base_url: str = "http://localhost:3334/api"
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "application/json"})
    k_values: list[int] = field(default_factory=lambda: [1, 3, 5, 10])
    timeout_seconds: float = 30.0
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
    search_type: SearchType
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> dict[str, Any]:
        """Convert the report to a JSON-safe dictionary."""
        return {
            "timestamp": self.timestamp,
            "search_type": self.search_type,
            "num_queries": len(self.queries),
            "metrics": self.aggregated.to_dict(),
            "per_query": [
                {
                    "query": result.query.query,
                    "metrics": result.metrics.to_dict(),
                    "error": result.error,
                }
                for result in self.queries
            ],
        }

    def save(self, path: Path | None = None) -> Path:
        """Save the report to disk."""
        if path is None:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            filename = f"eval_{self.search_type}_{time.strftime('%Y%m%d_%H%M%S')}.json"
            path = self.config.output_dir / filename

        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        log.info("Saved evaluation report", path=str(path))
        return path

    def print_summary(self) -> None:
        """Print a compact summary for interactive runs."""
        metrics = self.aggregated.to_dict()
        print(f"\nSibyl evaluation summary ({self.search_type})")
        print(f"  queries: {len(self.queries)}")
        print(f"  ndcg@10: {metrics['ndcg@10']:.3f}")
        print(f"  success@5: {metrics['success@5']:.3f}")
        print(f"  mrr: {metrics['mrr']:.3f}")
        print(f"  latency_ms: {metrics['latency_ms']:.1f}")


def load_queries(path: Path) -> list[EvalQuery]:
    """Load evaluation queries from a JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        EvalQuery(
            query=item["query"],
            expected_ids=item.get("expected_ids", []),
            relevance_grades=item.get("relevance_grades", {}),
            metadata=item.get("metadata", {}),
        )
        for item in data["queries"]
    ]


def get_sample_queries() -> list[EvalQuery]:
    """Return sample queries for smoke-testing the harness itself."""
    return [
        EvalQuery(
            query="How to install FastAPI",
            expected_ids=["fastapi-install-1", "fastapi-quickstart-1"],
            relevance_grades={"fastapi-install-1": 3, "fastapi-quickstart-1": 2},
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
            relevance_grades={"sqlalchemy-pool-1": 3, "async-db-1": 2},
        ),
        EvalQuery(
            query="error handling patterns async await",
            expected_ids=["async-errors-1", "exception-patterns-1"],
            relevance_grades={"async-errors-1": 3, "exception-patterns-1": 2},
        ),
        EvalQuery(
            query="GraphQL subscription implementation",
            expected_ids=["graphql-subscriptions-1"],
            relevance_grades={"graphql-subscriptions-1": 3},
        ),
    ]


class EvalRunner:
    """Runner for live Sibyl evaluation queries."""

    def __init__(self, config: EvalConfig | None = None):
        self.config = config or EvalConfig()
        self._http_client: httpx.AsyncClient | Any | None = None

    async def _get_client(self) -> httpx.AsyncClient | Any:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self.config.api_base_url,
                headers=self.config.headers,
                timeout=self.config.timeout_seconds,
            )
        return self._http_client

    async def close(self) -> None:
        """Close the underlying HTTP client if needed."""
        if self._http_client is not None and hasattr(self._http_client, "aclose"):
            await self._http_client.aclose()
        self._http_client = None

    def _build_request(
        self, query: EvalQuery, search_type: SearchType
    ) -> tuple[str, dict[str, Any]]:
        if search_type == "unified":
            return "/search", {"query": query.query, "limit": 20, "include_content": True}
        if search_type == "code-examples":
            return "/rag/code-examples", {"query": query.query, "match_count": 20}
        if search_type == "hybrid":
            return "/rag/hybrid-search", {"query": query.query, "match_count": 20}
        return "/rag/search", {"query": query.query, "match_count": 20}

    def _parse_results(
        self, data: dict[str, Any], search_type: SearchType
    ) -> list[RetrievalResult]:
        if search_type == "code-examples":
            return [
                RetrievalResult(
                    id=item.get("chunk_id", ""),
                    content=item.get("code", ""),
                    score=item.get("similarity", 0.0),
                    metadata={"language": item.get("language"), "url": item.get("url")},
                )
                for item in data.get("examples", [])
            ]

        if search_type == "unified":
            return [
                RetrievalResult(
                    id=item.get("id", ""),
                    content=item.get("content", ""),
                    score=item.get("score", 0.0),
                    metadata={
                        "type": item.get("type"),
                        "result_origin": item.get("result_origin"),
                        "source": item.get("source"),
                        "url": item.get("url"),
                    },
                )
                for item in data.get("results", [])
            ]

        return [
            RetrievalResult(
                id=item.get("chunk_id", item.get("document_id", "")),
                content=item.get("content", ""),
                score=item.get("similarity", 0.0),
                metadata={
                    "document_id": item.get("document_id"),
                    "source_id": item.get("source_id"),
                    "source_name": item.get("source_name"),
                    "url": item.get("url"),
                },
            )
            for item in data.get("results", [])
        ]

    async def run_query(self, query: EvalQuery, search_type: SearchType = "unified") -> EvalResult:
        """Run a single evaluation query against a live Sibyl endpoint."""
        client = await self._get_client()
        endpoint, payload = self._build_request(query, search_type)

        start_time = time.perf_counter()
        results: list[RetrievalResult] = []
        error = None

        try:
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            results = self._parse_results(response.json(), search_type)
        except Exception as exc:
            error = str(exc)
            log.exception("evaluation_query_failed", query=query.query, error=error)

        latency_ms = (time.perf_counter() - start_time) * 1000
        metrics = compute_metrics(
            results=results,
            query=query,
            latency_ms=latency_ms,
            k_values=self.config.k_values,
        )
        return EvalResult(query=query, results=results, metrics=metrics, error=error)

    async def run_evaluation(
        self,
        queries: list[EvalQuery],
        search_type: SearchType = "unified",
    ) -> EvalReport:
        """Run a full evaluation and aggregate the results."""
        log.info("Starting evaluation", num_queries=len(queries), search_type=search_type)
        results = []
        for index, query in enumerate(queries, start=1):
            log.debug("Running query", index=index, total=len(queries), query=query.query[:50])
            results.append(await self.run_query(query, search_type))

        report = EvalReport(
            config=self.config,
            queries=results,
            aggregated=aggregate_metrics([result.metrics for result in results]),
            search_type=search_type,
        )
        if self.config.save_results:
            report.save()

        log.info(
            "Evaluation complete",
            num_queries=len(queries),
            search_type=search_type,
            ndcg_at_10=report.aggregated.ndcg_at_k.get(10, 0.0),
            success_at_5=report.aggregated.success_at_k.get(5, 0.0),
        )
        return report

    async def __aenter__(self) -> EvalRunner:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


async def run_evaluation_cli(
    queries_file: Path | None = None,
    search_type: SearchType = "unified",
    config: EvalConfig | None = None,
) -> EvalReport:
    """Run an evaluation from a query file or the built-in sample set."""
    queries = load_queries(queries_file) if queries_file else get_sample_queries()
    async with EvalRunner(config) as runner:
        report = await runner.run_evaluation(queries, search_type)
        report.print_summary()
        return report
