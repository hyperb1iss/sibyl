#!/usr/bin/env python3
"""Replay LongMemEval live artifacts with offline reranking experiments."""

# ruff: noqa: T201

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "sibyl-core" / "src"))

from sibyl_core.evals.longmemeval import (  # noqa: E402
    USER_AND_ASSISTANT_CORPUS_TEXT_POLICY,
)
from sibyl_core.evals.longmemeval_replay import (  # noqa: E402
    load_longmemeval_replay_inputs,
    longmemeval_rerank_feature_rows,
    replay_longmemeval_report,
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


def _report_text_policy(report: Mapping[str, Any]) -> str:
    dataset = report.get("dataset")
    if isinstance(dataset, Mapping):
        policy = dataset.get("corpus_text_policy")
        if policy:
            return str(policy)
    return USER_AND_ASSISTANT_CORPUS_TEXT_POLICY


def _iter_feature_rows(
    report: Mapping[str, Any],
    dataset: Sequence[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    text_policy = _report_text_policy(report)
    for case in report.get("case_results", []):
        if not isinstance(case, Mapping):
            continue
        case_index = int(case["case_index"])
        entry = dataset[case_index]
        for row in longmemeval_rerank_feature_rows(
            case,
            entry,
            corpus_text_policy=text_policy,
        ):
            yield {
                "case_index": case_index,
                "question_id": case.get("question_id"),
                "question_type": case.get("question_type"),
                "question": case.get("question"),
                "question_date": case.get("question_date"),
                "answer_session_ids": [
                    str(session_id) for session_id in case.get("answer_session_ids", [])
                ],
                **row,
            }


def _write_feature_rows(
    path: Path,
    report: Mapping[str, Any],
    dataset: Sequence[Mapping[str, Any]],
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as output:
        for row in _iter_feature_rows(report, dataset):
            output.write(json.dumps(row, sort_keys=True))
            output.write("\n")
            count += 1
    return count


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
    parser.add_argument(
        "--features-jsonl",
        type=Path,
        help="Write labeled heuristic reranker feature rows to a JSONL file",
    )
    args = parser.parse_args(argv)

    report, dataset = load_longmemeval_replay_inputs(
        args.report,
        dataset_path=args.dataset,
    )
    summary = replay_longmemeval_report(
        report,
        dataset,
        strategy=args.strategy,
        k_values=args.k_values,
    )
    payload = summary_to_dict(summary, include_cases=args.include_cases)
    if args.features_jsonl:
        feature_rows = _write_feature_rows(args.features_jsonl, report, dataset)
        payload["feature_rows"] = {
            "count": feature_rows,
            "path": str(args.features_jsonl),
        }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_text_summary(payload)
        if args.features_jsonl:
            print(f"feature_rows: wrote {feature_rows} to {args.features_jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
