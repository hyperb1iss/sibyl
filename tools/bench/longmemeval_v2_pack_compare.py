#!/usr/bin/env python3
"""Compare LongMemEval-V2 retrieval packs with strict entity identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "sibyl-longmemeval-v2-pack-comparison-v1"


class PackInputError(ValueError):
    """Raised when a retrieval row cannot support an honest comparison."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def load_packs(  # noqa: PLR0912
    path: Path,
    *,
    wanted: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    packs: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PackInputError(f"{path}:{line_number} is not valid JSON") from exc
            if not isinstance(row, dict):
                raise PackInputError(f"{path}:{line_number} is not an object")
            question_id = str(row.get("question_id") or "").strip()
            if not question_id or (wanted is not None and question_id not in wanted):
                continue
            if question_id in packs:
                raise PackInputError(f"{path} repeats question_id {question_id!r}")
            if row.get("row_status", "valid") != "valid":
                raise PackInputError(f"{path} question {question_id!r} is not a valid row")
            metadata = row.get("memory_post_query_metadata")
            if not isinstance(metadata, dict):
                question_item = row.get("question_item")
                metadata = (
                    question_item.get("memory_post_query_metadata")
                    if isinstance(question_item, dict)
                    else None
                )
            if not isinstance(metadata, dict):
                raise PackInputError(f"{path} question {question_id!r} has no retrieval metadata")
            trace = metadata.get("retrieval_trace")
            if not isinstance(trace, list) or not trace:
                raise PackInputError(f"{path} question {question_id!r} has an empty trace")
            entity_ids: list[str] = []
            for item in trace:
                entity_id = (
                    str(item.get("entity_id") or "").strip() if isinstance(item, dict) else ""
                )
                if not entity_id:
                    raise PackInputError(
                        f"{path} question {question_id!r} has a trace item without entity_id"
                    )
                entity_ids.append(entity_id)
            latency = row.get("memory_query_duration_seconds")
            packs[question_id] = {
                "entity_ids": entity_ids,
                "latency_seconds": (
                    float(latency)
                    if isinstance(latency, int | float) and not isinstance(latency, bool)
                    else None
                ),
            }
    if wanted is not None and set(packs) != wanted:
        missing = sorted(wanted - set(packs))
        raise PackInputError(f"{path} is missing requested questions: {missing}")
    if not packs:
        raise PackInputError(f"{path} has no comparable retrieval rows")
    return packs


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise PackInputError("cannot calculate a percentile over no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def compare_packs(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if set(left) != set(right):
        raise PackInputError(
            "pack inputs have different question sets: "
            f"left_only={sorted(set(left) - set(right))}, "
            f"right_only={sorted(set(right) - set(left))}"
        )
    rows: list[dict[str, Any]] = []
    for question_id in sorted(left):
        left_ids = left[question_id]["entity_ids"]
        right_ids = right[question_id]["entity_ids"]
        left_set = set(left_ids)
        right_set = set(right_ids)
        union = left_set | right_set
        latency_left = left[question_id]["latency_seconds"]
        latency_right = right[question_id]["latency_seconds"]
        rows.append(
            {
                "question_id": question_id,
                "ordered_identical": left_ids == right_ids,
                "membership_identical": left_set == right_set,
                "jaccard": len(left_set & right_set) / len(union),
                "latency_delta_seconds": (
                    latency_left - latency_right
                    if latency_left is not None and latency_right is not None
                    else None
                ),
            }
        )
    jaccards = [float(row["jaccard"]) for row in rows]
    latency_deltas = [
        float(row["latency_delta_seconds"])
        for row in rows
        if row["latency_delta_seconds"] is not None
    ]
    ordered_identical_count = sum(bool(row["ordered_identical"]) for row in rows)
    membership_identical_count = sum(bool(row["membership_identical"]) for row in rows)
    return {
        "question_count": len(rows),
        "ordered_identical_count": ordered_identical_count,
        "membership_identical_count": membership_identical_count,
        "divergent_count": len(rows) - ordered_identical_count,
        "jaccard": {
            "minimum": min(jaccards),
            "p05": percentile(jaccards, 0.05),
            "p50": percentile(jaccards, 0.50),
        },
        "latency_delta_seconds": (
            {
                "mean": sum(latency_deltas) / len(latency_deltas),
                "p05": percentile(latency_deltas, 0.05),
                "p50": percentile(latency_deltas, 0.50),
                "p95": percentile(latency_deltas, 0.95),
            }
            if latency_deltas
            else None
        ),
        "rows": rows,
    }


def build_receipt(
    left_path: Path,
    right_path: Path,
    *,
    wanted: set[str] | None = None,
) -> dict[str, Any]:
    comparison = compare_packs(
        load_packs(left_path, wanted=wanted),
        load_packs(right_path, wanted=wanted),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "IDENTICAL" if comparison["divergent_count"] == 0 else "DIVERGENT",
        "inputs": {
            "left": {"path": str(left_path), "sha256": sha256_file(left_path)},
            "right": {"path": str(right_path), "sha256": sha256_file(right_path)},
        },
        "comparison": comparison,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--questions", help="Comma-separated question IDs")
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    wanted = set(args.questions.split(",")) if args.questions else None
    try:
        receipt = build_receipt(Path(args.left), Path(args.right), wanted=wanted)
    except (OSError, PackInputError) as exc:
        print(f"pack comparison failed: {exc}", file=sys.stderr)  # noqa: T201
        return 2
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")  # noqa: T201
    return 0 if receipt["status"] == "IDENTICAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
