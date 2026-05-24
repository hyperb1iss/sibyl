#!/usr/bin/env python3
"""Inspect local LongMemEval-V2 inputs before running a full harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "sibyl-core" / "src"))

from sibyl_core.evals.longmemeval_v2 import (  # noqa: E402
    load_longmemeval_v2_haystack,
    load_longmemeval_v2_questions,
    select_longmemeval_v2_trajectories,
    summarize_longmemeval_v2_inputs,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe local LongMemEval-V2 data files.")
    parser.add_argument("data_root", help="Directory containing questions.jsonl and haystacks")
    parser.add_argument("--tier", choices=("small", "medium"), default="small")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--validate-trajectories",
        action="store_true",
        help="Stream trajectories.jsonl and validate selected haystack ids.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    args = parser.parse_args(argv)

    data_root = Path(args.data_root)
    questions_path = data_root / "questions.jsonl"
    haystack_path = _haystack_path(data_root, args.tier)
    questions = load_longmemeval_v2_questions(questions_path)
    haystack = load_longmemeval_v2_haystack(haystack_path)
    selected_questions = questions[: args.limit] if args.limit is not None else questions
    selected_haystack = {
        question.id: haystack.get(question.id, []) for question in selected_questions
    }
    trajectories = None
    if args.validate_trajectories:
        trajectory_path = data_root / "trajectories.jsonl"
        trajectory_ids = {
            trajectory_id
            for ids in selected_haystack.values()
            for trajectory_id in ids
        }
        trajectories = select_longmemeval_v2_trajectories(trajectory_path, trajectory_ids)

    summary = {
        "schema_version": "sibyl-longmemeval-v2-probe-v1",
        "tier": args.tier,
        "data_root": str(data_root),
        "questions_path": str(questions_path),
        "haystack_path": str(haystack_path),
        "limit": args.limit,
        **summarize_longmemeval_v2_inputs(
            selected_questions,
            selected_haystack,
            trajectories=trajectories,
        ),
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_summary(summary)
    return _exit_code(summary)


def _haystack_path(data_root: Path, tier: str) -> Path:
    nested = data_root / "haystacks" / f"lme_v2_{tier}.json"
    if nested.exists():
        return nested
    flat = data_root / f"lme_v2_{tier}.json"
    if flat.exists():
        return flat
    return nested


def _print_summary(summary: dict[str, Any]) -> None:
    print("LongMemEval-V2 probe")
    print(f"  tier: {summary['tier']}")
    print(f"  questions: {summary['question_count']}")
    print(f"  haystacks: {summary['haystack_count']}")
    print(f"  haystack size: {summary['haystack_min']}..{summary['haystack_max']}")
    print(f"  domains: {summary['domain_counts']}")
    print(f"  question types: {summary['question_type_counts']}")
    if "trajectory_count" in summary:
        print(f"  selected trajectories loaded: {summary['trajectory_count']}")
        print(f"  missing trajectory ids: {summary['missing_trajectory_count']}")
    if summary["missing_haystack_questions"]:
        print(f"  missing haystacks: {len(summary['missing_haystack_questions'])}")
    if summary["orphan_haystack_questions"]:
        print(f"  orphan haystacks: {len(summary['orphan_haystack_questions'])}")


def _exit_code(summary: dict[str, Any]) -> int:
    if summary["missing_haystack_questions"] or summary["orphan_haystack_questions"]:
        return 1
    if summary.get("missing_trajectory_count", 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
