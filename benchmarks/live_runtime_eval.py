#!/usr/bin/env python3
"""Evaluate the live Sibyl runtime path against `/api/search` or RAG endpoints.

This harness talks to a running Sibyl stack and measures the actual HTTP search
surfaces instead of an ephemeral in-memory baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "sibyl-core" / "src"))
sys.path.insert(0, str(ROOT / "apps" / "cli" / "src"))

from sibyl_core.evals import EvalConfig, run_evaluation_cli


def _get_client_headers() -> dict[str, str]:
    try:
        from sibyl_cli.client import SibylClient

        return SibylClient()._default_headers()
    except Exception:
        return {"Content-Type": "application/json"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the live Sibyl runtime search surfaces.")
    parser.add_argument(
        "queries",
        nargs="?",
        type=Path,
        help="Path to a JSON file with eval queries. Falls back to built-in sample queries.",
    )
    parser.add_argument(
        "--search-type",
        choices=["unified", "rag", "hybrid", "code-examples"],
        default="unified",
        help="Which live API surface to evaluate.",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:3334/api",
        help="Base Sibyl API URL.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results",
        help="Directory for saved evaluation reports.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Print the report summary without writing a JSON artifact.",
    )
    args = parser.parse_args()

    config = EvalConfig(
        api_base_url=args.api_url,
        headers=_get_client_headers(),
        output_dir=args.output_dir,
        save_results=not args.no_save,
    )
    asyncio.run(
        run_evaluation_cli(
            queries_file=args.queries,
            search_type=args.search_type,
            config=config,
        )
    )


if __name__ == "__main__":
    main()
