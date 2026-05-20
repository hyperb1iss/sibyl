#!/usr/bin/env python3
"""Replay LongMemEval live artifacts with offline reranking experiments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "sibyl-core" / "src"))

from sibyl_core.evals.longmemeval_replay import (  # noqa: E402
    replay_longmemeval_report_path,
    summary_to_dict,
)


def _parse_k_values(value: str) -> list[int]:
    values = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("Expected at least one k value")
    if any(k <= 0 for k in values):
        raise argparse.ArgumentTypeError("k values must be positive")
    return sorted(set(values))


def _format_percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def _print_text_summary(payload: dict[str, Any]) -> None:
    baseline = payload["baseline_overall"]
    overall = payload["overall"]
    delta = payload["delta"]
    print(f"strategy: {payload['strategy']}")
    for key in sorted(overall):
        print(
            f"{key}: {_format_percent(baseline[key])} -> "
            f"{_format_percent(overall[key])} ({delta[key] * 100:+.2f} pts)"
        )
    print(
        "cases: "
        f"changed={payload['changed_cases']} "
        f"improved={payload['improved_cases']} "
        f"regressed={payload['regressed_cases']}"
    )
    print("per_type recall@5:")
    for question_type, metrics in payload["per_type"].items():
        baseline_metrics = payload["baseline_per_type"][question_type]
        before = baseline_metrics.get("recall@5", 0.0)
        after = metrics.get("recall@5", 0.0)
        print(
            f"  {question_type}: {_format_percent(before)} -> "
            f"{_format_percent(after)} ({(after - before) * 100:+.2f} pts)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a LongMemEval live report with an offline reranking strategy."
    )
    parser.add_argument("report", help="Path to longmemeval_live_full.json")
    parser.add_argument("--dataset", help="Override dataset path from report metadata")
    parser.add_argument(
        "--strategy",
        choices=["identity", "heuristic", "coverage", "oracle"],
        default="heuristic",
        help="Reranking strategy to apply",
    )
    parser.add_argument(
        "--k-values",
        type=_parse_k_values,
        default=None,
        help="Comma-separated k values to score, default from report",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a text summary",
    )
    parser.add_argument(
        "--include-cases",
        action="store_true",
        help="Include per-case replay details in JSON output",
    )
    args = parser.parse_args(argv)

    summary = replay_longmemeval_report_path(
        args.report,
        dataset_path=args.dataset,
        strategy=args.strategy,
        k_values=args.k_values,
    )
    payload = summary_to_dict(summary, include_cases=args.include_cases)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_text_summary(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
