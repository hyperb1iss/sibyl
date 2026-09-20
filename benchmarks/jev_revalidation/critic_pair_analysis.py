"""Bind a retrospective full-critic comparison to frozen source-support decisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

from sibyl_core.tasks import source_support
from sibyl_core.tasks._evidence_json import canonical, read_json_value

from . import critic_pair, support_inputs, support_study
from . import support_fallback_policy as policy
from . import support_fallback_study as fallback

VERSION = "jev-critic-pair-v1"


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def build_plan(cases_path: Path, run_path: Path, selection_path: Path) -> dict[str, Any]:
    """Revalidate original receipts and select every primary bypass without refitting."""
    selection = read_json_value(selection_path.read_bytes())
    fallback._validate_selection(selection)
    if hashlib.sha256(cases_path.read_bytes()).hexdigest() != selection["holdout_cases_sha256"]:
        raise ValueError("held-out fixture differs from frozen selection")
    cases, rows, bindings = fallback._load_run(cases_path, run_path)
    for key in ("program_hashes", "route_policy_sha256", "current_code_hashes"):
        if bindings[key] != selection["calibration"][key]:
            raise ValueError(f"held-out control mismatch: {key}")
    left = selection["calibration"]["case_fingerprints"]
    right = bindings["case_fingerprints"]
    if set(left) & set(right) or set(left.values()) & set(right.values()):
        raise ValueError("calibration and holdout overlap")
    selected = {
        (row["case_id"], row["repeat"])
        for row in rows
        if row["arm"] == "grouped" and policy.route(row, selection["threshold"])[0] == "bypass"
    }
    manifest = read_json_value((run_path / "manifest.json").read_bytes())
    scheduled = support_study.schedule(
        cases, manifest["run_id"], repeats=manifest["repeats"], seed=manifest["seed"]
    )
    calls = []
    by_id = {case["id"]: (index, case) for index, case in enumerate(cases)}
    for call in scheduled:
        if call.arm != "grouped" or (call.case_id, call.repeat) not in selected:
            continue
        index, case = by_id[call.case_id]
        run_id = f"{manifest['run_id']}:case-{index}-repeat-{call.repeat}"
        prepared = critic_pair.prepare_case(case, run_id)
        semantic = read_json_value(prepared.payload_json.encode())
        semantic.pop("parent_operation_id")
        semantic.pop("parent_candidate_sha256")
        source_support._semantic_evidence(semantic)
        if canonical(semantic) != call.request.state:
            raise ValueError("critic input differs from retained semantic evidence")
        request = critic_pair.critic_request(prepared)
        regenerated = support_inputs.make_request(case, run_id)
        if regenerated.state != call.request.state:
            raise ValueError("prepared semantic evidence differs from retained Jev input")
        calls.append(
            {
                "id": call.key,
                "case_id": call.case_id,
                "repeat": call.repeat,
                "case": case,
                "preparation_run_id": run_id,
                "expected_jev_state_sha256": text_digest(call.request.state),
                "prepared_payload_sha256": text_digest(prepared.payload_json),
                "prompt_sha256": text_digest(prepared.prompt),
                "request_sha256": digest(request),
            }
        )
    if not calls or len(calls) != len(selected):
        raise ValueError("paired comparison requires complete nonempty bypass selection")
    accounting = bindings["accounting"]["grouped"]
    if accounting["unknown_observed_cost_usd_calls"]:
        raise ValueError("original routing cost is incomplete")
    result = {
        "version": VERSION,
        "controls": deepcopy(critic_pair.CONTROLS),
        "source": {
            "cases_path": str(cases_path.resolve()),
            "run_path": str(run_path.resolve()),
            "selection_path": str(selection_path.resolve()),
        },
        "selection_sha256": selection["selection_sha256"],
        "selection_file_sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
        "upstream_bindings": bindings,
        "routing_cost_usd": accounting["observed_cost_usd"],
        "routing_calls": accounting["scheduled_calls"],
        "candidate_evaluations": sum(row["arm"] == "grouped" for row in rows),
        "calls": calls,
        "code_hashes": {
            "critic_pair.py": hashlib.sha256(Path(critic_pair.__file__).read_bytes()).hexdigest(),
            "critic_pair_analysis.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    }
    result["plan_sha256"] = digest(result)
    return result


def validate_plan(plan: dict[str, Any]) -> None:
    """Compare against fresh receipt reconstruction, not a self-consistent edited plan."""
    source = plan["source"]
    expected = build_plan(
        Path(source["cases_path"]), Path(source["run_path"]), Path(source["selection_path"])
    )
    if expected != plan:
        raise ValueError("paired plan changed or differs from original frozen inputs")


def summarize(plan: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Account for every selected call; findings and unknown spend block qualification."""
    expected = {call["id"]: call for call in plan["calls"]}
    actual = {receipt["id"]: receipt for receipt in receipts}
    if len(actual) != len(receipts) or actual.keys() != expected.keys():
        raise ValueError("paired receipts must cover the selected calls exactly once")
    outcomes = []
    known_cost = Decimal("0")
    unknown_cost = 0
    for key, call in expected.items():
        receipt = actual[key]
        if receipt["request_sha256"] != call["request_sha256"]:
            raise ValueError("paired receipt request mismatch")
        cost = receipt["usage"]["observed_cost_usd"]
        if cost is None:
            unknown_cost += 1
        elif type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            raise ValueError("invalid observed critic cost")
        else:
            known_cost += Decimal(str(cost))
        result = receipt.get("result")
        status = (
            result["status"]
            if receipt["execution_status"] == "completed" and result
            else receipt["execution_status"]
        )
        outcomes.append(
            {
                "id": key,
                "case_id": call["case_id"],
                "repeat": call["repeat"],
                "critic_status": status,
                "agrees_with_bypass": status == "no_findings",
                "observed_cost_usd": cost,
                "elapsed_ms": receipt["elapsed_ms"],
                "result": result,
            }
        )
    agreement = sum(row["agrees_with_bypass"] for row in outcomes)
    qualified = agreement == len(outcomes) and unknown_cost == 0
    routing_cost = plan["routing_cost_usd"]
    if (
        type(routing_cost) not in (int, float)
        or not math.isfinite(routing_cost)
        or routing_cost < 0
    ):
        raise ValueError("invalid original routing cost")
    difference = float(known_cost - Decimal(str(routing_cost))) if unknown_cost == 0 else None
    return {
        "plan_sha256": plan["plan_sha256"],
        "selected_evaluations": len(outcomes),
        "selected_distinct_candidates": len({row["case_id"] for row in outcomes}),
        "critic_statuses": dict(sorted(Counter(row["critic_status"] for row in outcomes).items())),
        "agreement": agreement,
        "disagreement_or_unavailable": len(outcomes) - agreement,
        "known_observed_critic_cost_usd": float(known_cost),
        "unknown_critic_cost_calls": unknown_cost,
        "all_grouped_routing_cost_usd": routing_cost,
        "all_grouped_routing_calls": plan["routing_calls"],
        "measured_experiment_cost_usd": float(known_cost) if not unknown_cost else None,
        "unqualified_cost_difference_usd": difference,
        "conditional_avoided_cost_usd": difference
        if qualified and difference is not None and difference > 0
        else None,
        "qualification": "conditional_agreement"
        if qualified and difference is not None and difference > 0
        else "not_qualified",
        "candidate_outcomes": outcomes,
        "limits": [
            "Retrospective selected synthetic plain reflections; critic agreement is not independent truth or fresh safety evidence.",
            "All prior grouped Jev calls are charged; unchanged fallback critic and correction costs cancel only under an untested counterfactual assumption.",
            "Production prompt, schema and mechanical validation are reused through different OpenRouter transport and generation settings.",
            "No production activation, publication authority, complete-eval savings percentage, or end-to-end latency measurement.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    plan = build_plan(args.cases, args.run, args.selection)
    with args.out.open("x") as stream:
        json.dump(plan, stream, indent=2, allow_nan=False)
        stream.write("\n")
    sys.stdout.write(
        json.dumps({"calls": len(plan["calls"]), "plan_sha256": plan["plan_sha256"]}) + "\n"
    )


if __name__ == "__main__":
    main()
