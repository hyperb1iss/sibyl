"""Controlled synthetic comparison of source prompts with oracle claim kinds."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from .source_study import (
    _accounting,
    _bytes,
    _digest,
    _method_row,
    _read_run,
    _write,
    load_cases,
    metrics,
    projections,
    typed_disposition,
)

_KINDS = frozenset({"current_state", "standing_rule", "historical", "unknown"})
_ARMS = ("v3", "unknown", "typed")


def kind_projections(cases: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    for case in cases:
        if case.get("claim_kind") not in _KINDS:
            raise ValueError("claim-kind study requires an explicit recognized claim_kind")
        if "model_claim_kind" in case:
            raise ValueError("master fixture cannot already contain model_claim_kind")
    _, source = projections(cases)
    return {
        "v3": source,
        "unknown": [{**case, "model_claim_kind": "unknown"} for case in source],
        "typed": [{**case, "model_claim_kind": case["claim_kind"]} for case in source],
    }


def prepare(cases_path: Path, out: Path) -> dict[str, Any]:
    cases = load_cases(cases_path)
    arms = kind_projections(cases)
    manifest = {
        "master_sha256": _digest(cases_path.read_bytes()),
        "kind_study_sha256": _digest(Path(__file__).read_bytes()),
        "case_count": len(cases),
        "projections_sha256": {
            name: _digest(_bytes(projected)) for name, projected in arms.items()
        },
    }
    out.mkdir(parents=True, exist_ok=False)
    for name, projected in arms.items():
        _write(out / f"{name}.json", projected)
    _write(out / "manifest.json", manifest)
    return manifest


def _validate_controls(manifests: dict[str, dict[str, Any]]) -> int:
    base = manifests["v3"]
    route = base.get("route_policy_sha256")
    if not isinstance(route, str) or re.fullmatch(r"[0-9a-f]{64}", route) is None:
        raise ValueError("claim-kind runs require a valid route policy")
    for name in _ARMS:
        current = manifests[name]
        for key in ("route_policy_sha256", "repeats", "batch_size", "concurrency"):
            if key not in base or current.get(key) != base[key]:
                raise ValueError(f"claim-kind control mismatch: {key}")
        if type(current["concurrency"]) is not int or current["concurrency"] < 1:
            raise ValueError("claim-kind runs require positive concurrency")
    return base["repeats"]


def _score(
    cases: list[dict[str, Any]], grid: dict[tuple[str, int], dict[str, Any]], repeats: int
) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        for repeat in range(repeats):
            prediction = grid[case["id"], repeat]
            relation = prediction["relation"] if prediction["status"] == "completed" else None
            action = typed_disposition(relation, case) if relation is not None else None
            row = _method_row(case, repeat, action)
            row.update(
                relation=relation,
                expected_source_relation=case["expected_source_relation"],
                claim_kind=case["claim_kind"],
                event_extent=case.get("event_extent"),
                pair_group=case.get("pair_group") or case["id"],
            )
            rows.append(row)
    return rows


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = metrics(rows)
    retained = sum(row["action"] == "retain" for row in rows)
    result.update(retain_count=retained, retention_rate=retained / len(rows) if rows else None)
    return result


def _joint_groups(rows: list[dict[str, Any]], repeats: int) -> dict[str, Any]:
    result = {}
    for repeat in range(repeats):
        selected = [row for row in rows if row["repeat"] == repeat]
        groups = {}
        for group in sorted({row["pair_group"] for row in selected}):
            members = [row for row in selected if row["pair_group"] == group]
            groups[group] = {
                "joint_correct": all(
                    row["action"] == row["expected_disposition"] for row in members
                ),
                "cases": len(members),
                "outcomes": [
                    {
                        key: row[key]
                        for key in ("case_id", "action", "expected_disposition", "status")
                    }
                    for row in members
                ],
            }
        correct = sum(group["joint_correct"] for group in groups.values())
        result[str(repeat)] = {
            "groups": len(groups),
            "joint_correct_groups": correct,
            "joint_accuracy": correct / len(groups) if groups else None,
            "outcomes": groups,
        }
    return result


def _group_metrics(rows: list[dict[str, Any]], repeats: int) -> dict[str, Any]:
    subsets = {
        "current_state_temporary_gold_overlay": [
            row
            for row in rows
            if row["claim_kind"] == "current_state"
            and row["event_extent"] == "temporary"
            and row["expected_disposition"] == "overlay"
        ],
        "standing_rule_temporary": [
            row
            for row in rows
            if row["claim_kind"] == "standing_rule" and row["event_extent"] == "temporary"
        ],
    }
    return {
        "pooled": _metrics(rows),
        "repeats": {
            str(repeat): _metrics([row for row in rows if row["repeat"] == repeat])
            for repeat in range(repeats)
        },
        "claim_kinds": {
            kind: _metrics([row for row in rows if row["claim_kind"] == kind])
            for kind in sorted({row["claim_kind"] for row in rows})
        },
        "subsets": {
            name: {
                "pooled": _metrics(selected),
                "repeats": {
                    str(repeat): _metrics([row for row in selected if row["repeat"] == repeat])
                    for repeat in range(repeats)
                },
            }
            for name, selected in subsets.items()
        },
        "joint_group_correctness": _joint_groups(rows, repeats),
    }


def _pairwise(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    improvements = []
    regressions = []
    changed = []
    for left, right in zip(before, after, strict=True):
        if (left["case_id"], left["repeat"]) != (right["case_id"], right["repeat"]):
            raise ValueError("pairwise comparison identity mismatch")
        detail = {
            "case_id": left["case_id"],
            "repeat": left["repeat"],
            "expected_disposition": left["expected_disposition"],
            "before_action": left["action"],
            "after_action": right["action"],
            "before_status": left["status"],
            "after_status": right["status"],
        }
        left_correct = left["action"] == left["expected_disposition"]
        right_correct = right["action"] == right["expected_disposition"]
        if not left_correct and right_correct:
            improvements.append(detail)
        elif left_correct and not right_correct:
            regressions.append(detail)
        if left["action"] != right["action"] or left["relation"] != right["relation"]:
            changed.append(detail)
    return {
        "cases": len(before),
        "improvement_count": len(improvements),
        "regression_count": len(regressions),
        "improvements": improvements,
        "regressions": regressions,
        "changed": changed,
        "both_complete": sum(
            left["status"] == right["status"] == "completed"
            for left, right in zip(before, after, strict=True)
        ),
    }


def analyze(
    cases_path: Path, v3_run: Path, unknown_run: Path, typed_run: Path, out: Path
) -> dict[str, Any]:
    cases = load_cases(cases_path)
    projected = kind_projections(cases)
    paths = {"v3": v3_run, "unknown": unknown_run, "typed": typed_run}
    manifests = {}
    grids = {}
    for name, path in paths.items():
        manifest, grid, _ = _read_run(
            path, projected[name], prompt_version="v3" if name == "v3" else "v4", arm="direct"
        )
        manifests[name] = manifest
        grids[name] = grid
    repeats = _validate_controls(manifests)
    scored = {name: _score(cases, grids[name], repeats) for name in _ARMS}
    result = {
        "master_sha256": _digest(cases_path.read_bytes()),
        "kind_study_sha256": _digest(Path(__file__).read_bytes()),
        "source_study_sha256": _digest(Path(__file__).with_name("source_study.py").read_bytes()),
        "unique_cases": len(cases),
        "repeats": repeats,
        "projections_sha256": {name: _digest(_bytes(projected[name])) for name in _ARMS},
        "run_paths": {name: str(path.resolve()) for name, path in paths.items()},
        "input_artifact_sha256": {
            name: {
                filename: _digest((path / filename).read_bytes())
                for filename in ("manifest.json", "predictions.json", "calls.json")
            }
            for name, path in paths.items()
        },
        "methods": {name: _group_metrics(scored[name], repeats) for name in _ARMS},
        "pairwise": {
            f"{before}_to_{after}": _pairwise(scored[before], scored[after])
            for before, after in (("v3", "unknown"), ("unknown", "typed"), ("v3", "typed"))
        },
        "errors": {
            name: [row for row in scored[name] if row["action"] != row["expected_disposition"]]
            for name in _ARMS
        },
        "accounting": {name: _accounting(paths[name], "direct", manifests[name]) for name in _ARMS},
        "limits": [
            "Claim kind is supplied by the fixture oracle; kind-extraction accuracy and cost are not measured.",
            "The same original claim_kind and temporal metadata drive offline policy in every arm.",
            "The unknown-to-typed comparison isolates the supplied kind; v3-to-unknown also changes the prompt.",
            "Repeated predictions share cases and scenario families; they are not independent samples.",
            "Synthetic fixture results do not establish production memory quality or safe automatic retirement.",
            "Failed measurements remain null and incorrect; accounting records original calls once per run.",
        ],
    }
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "analysis.json", result)
    _write(out / "predictions.json", scored)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--cases", type=Path, required=True)
    preparation.add_argument("--out", type=Path, required=True)
    analysis = commands.add_parser("analyze")
    analysis.add_argument("--cases", type=Path, required=True)
    analysis.add_argument("--v3-run", type=Path, required=True)
    analysis.add_argument("--unknown-run", type=Path, required=True)
    analysis.add_argument("--typed-run", type=Path, required=True)
    analysis.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = (
        prepare(args.cases, args.out)
        if args.command == "prepare"
        else analyze(args.cases, args.v3_run, args.unknown_run, args.typed_run, args.out)
    )
    sys.stdout.write(_bytes(result).decode())


if __name__ == "__main__":
    main()
