"""Compatibility wrapper around the shared Sibyl evaluation harness."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sibyl_core.evals import (
    EvalConfig,
    EvalQuery,
    EvalReport,
    EvalResult,
    EvalRunner,
    RetrievalResult,
    get_sample_queries,
    load_queries,
    run_evaluation_cli,
)

__all__ = [
    "EvalConfig",
    "EvalQuery",
    "EvalReport",
    "EvalResult",
    "EvalRunner",
    "RetrievalResult",
    "get_sample_queries",
    "load_queries",
    "run_evaluation_cli",
]


if __name__ == "__main__":
    queries_file = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(run_evaluation_cli(queries_file))
