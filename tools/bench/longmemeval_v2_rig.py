#!/usr/bin/env python3
"""Generate fail-closed Sibyl v1.3 LongMemEval-V2 decision receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.longmemeval_v2_official_source import (  # noqa: E402
    OFFICIAL_HARNESS_COMMIT,
)

RUN_PAIR_SCHEMA_VERSION = "sibyl-longmemeval-v2-paired-pass-v1"
PREREGISTRATION_SCHEMA_VERSION = "sibyl-longmemeval-v2-preregistration-v1"
AA_SCHEMA_VERSION = "sibyl-longmemeval-v2-aa-receipt-v1"
ANCHOR_SCHEMA_VERSION = "sibyl-longmemeval-v2-anchor-receipt-v1"
RACE_SCHEMA_VERSION = "sibyl-longmemeval-v2-machine-race-receipt-v1"
RENDER_SCHEMA_VERSION = "sibyl-longmemeval-v2-render-receipt-v1"
FAILURE_SCHEMA_VERSION = "sibyl-longmemeval-v2-rig-failure-v1"
DOMAINS = frozenset({"web", "enterprise"})
INITIAL_NOISE_FLOOR_PP = 3.0
GIT_SHA_LENGTH = 40
INITIAL_AA_PASS_COUNT = 3
EXTENDED_AA_PASS_COUNT = 5
PAIRED_PASS_COUNT = 3
NAIVE_MAXIMUM_LATENCY_RATIO = 0.60
RENDER_MINIMUM_EXPOSURE_GAIN_PP = 5.0
RENDER_MAXIMUM_LATENCY_REGRESSION_SECONDS = 2.0
RENDER_MAXIMUM_TOKEN_REGRESSION_RATIO = 0.25
RACE_DECISION_RULE = {
    "retention": "mean_delta_gte_noise_floor_and_every_pass_positive",
    "deletion_candidate": ("mean_delta_below_noise_floor_and_naive_latency_lte_60pct_each_pass"),
    "otherwise": "inconclusive_keep_machine_default",
    "deletion_in_v1_3": False,
}
RENDER_DECISION_RULE = {
    "minimum_exposure_gain_pp_each_domain": 5.0,
    "accuracy_gain": "mean_delta_gt_noise_floor_and_every_pass_positive",
    "maximum_mean_latency_regression_seconds": 2.0,
    "maximum_reader_token_regression_ratio": 0.25,
}


class RigInputError(ValueError):
    """Raised when an input cannot support a benchmark decision."""


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RigInputError(f"{path} is not a JSON object")
    return raw


def _nonempty_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RigInputError(f"{name} must be a non-empty string")
    return value.strip()


def _finite_number(value: object, *, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RigInputError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise RigInputError(f"{name} is outside its allowed range")
    return result


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RigInputError(f"{name} must be a positive integer")
    return value


def _mean(values: list[float]) -> float:
    if not values:
        raise RigInputError("cannot calculate a mean over no values")
    return sum(values) / len(values)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise RigInputError("cannot calculate a percentile over no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def validate_stack(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError("stack identity must be an object")
    commit = _nonempty_string(raw.get("sibyl_commit"), name="stack.sibyl_commit")
    if len(commit) != GIT_SHA_LENGTH or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise RigInputError("stack.sibyl_commit must be a full lowercase Git SHA")
    if raw.get("sibyl_git_status") != "clean":
        raise RigInputError("benchmark decisions require a clean Sibyl checkout")
    official = raw.get("official_source")
    if not isinstance(official, dict) or {
        "commit": official.get("commit"),
        "expected_commit": official.get("expected_commit"),
        "pin_matches": official.get("pin_matches"),
        "git_status": official.get("git_status"),
        "harness_exists": official.get("harness_exists"),
    } != {
        "commit": OFFICIAL_HARNESS_COMMIT,
        "expected_commit": OFFICIAL_HARNESS_COMMIT,
        "pin_matches": True,
        "git_status": "clean",
        "harness_exists": True,
    }:
        raise RigInputError("stack.official_source is not the clean reviewed harness pin")
    dataset = raw.get("dataset_sha256_by_domain")
    if not isinstance(dataset, dict) or set(dataset) != DOMAINS:
        raise RigInputError("stack dataset hashes must cover both Small domains")
    for domain, digest in dataset.items():
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise RigInputError(f"stack dataset hash for {domain} is invalid")
    for role in ("reader", "judge"):
        model = raw.get(role)
        if not isinstance(model, dict) or not model:
            raise RigInputError(f"stack.{role} model identity is missing")
    return dict(raw)


def stack_fingerprint(stack: dict[str, Any]) -> str:
    return canonical_sha256(stack)


def _validate_provider_usage(raw: object, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("complete") is not True:
        raise RigInputError(f"{name} provider usage is incomplete")
    _positive_int(raw.get("requests"), name=f"{name}.provider_usage.requests")
    total_tokens = raw.get("total_tokens")
    if isinstance(total_tokens, bool) or not isinstance(total_tokens, int) or total_tokens < 0:
        raise RigInputError(f"{name}.provider_usage.total_tokens is invalid")
    return dict(raw)


def _validate_activity(raw: object, *, mode: str, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} has no arm activity receipt")
    _positive_int(raw.get("activity_events"), name=f"{name}.activity_events")
    if mode == "naive":
        _positive_int(raw.get("naive_vector_attempts"), name=f"{name}.naive_vector_attempts")
    else:
        _positive_int(
            raw.get("hybrid_vector_attempts"),
            name=f"{name}.hybrid_vector_attempts",
        )
        if raw.get("typed_evidence_applicable") is not True:
            raise RigInputError(f"{name} does not mark typed evidence applicable")
    return dict(raw)


def validate_arm(  # noqa: PLR0912
    raw: object,
    *,
    stack_digest: str,
    side: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{side} arm must be an object")
    arm_name = _nonempty_string(raw.get("name"), name=f"{side}.name")
    configuration = raw.get("configuration")
    geometry = raw.get("geometry")
    if not isinstance(configuration, dict) or not configuration:
        raise RigInputError(f"{side}.configuration is missing")
    if not isinstance(geometry, dict):
        raise RigInputError(f"{side}.geometry is missing")
    _positive_int(geometry.get("max_context_items"), name=f"{side}.max_context_items")
    _positive_int(
        geometry.get("max_context_total_chars"),
        name=f"{side}.max_context_total_chars",
    )
    mode = _nonempty_string(configuration.get("retrieval_mode"), name=f"{side}.retrieval_mode")
    rows = raw.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RigInputError(f"{side}.rows is empty")
    seen: set[tuple[str, str]] = set()
    validated_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        name = f"{side}.rows[{index}]"
        if not isinstance(row, dict):
            raise RigInputError(f"{name} is not an object")
        if row.get("status") != "valid":
            raise RigInputError(f"{name} is failed or incomplete")
        domain = _nonempty_string(row.get("domain"), name=f"{name}.domain")
        if domain not in DOMAINS:
            raise RigInputError(f"{name}.domain is not a Small domain")
        question_id = _nonempty_string(row.get("question_id"), name=f"{name}.question_id")
        identity = (domain, question_id)
        if identity in seen:
            raise RigInputError(f"{side} repeats row {identity}")
        seen.add(identity)
        if not isinstance(row.get("score_bool"), bool):
            raise RigInputError(f"{name}.score_bool is not complete")
        _finite_number(row.get("latency_seconds"), name=f"{name}.latency_seconds", minimum=0)
        _positive_int(row.get("reader_tokens"), name=f"{name}.reader_tokens")
        if not isinstance(row.get("evidence_exposed"), bool):
            raise RigInputError(f"{name}.evidence_exposed must be boolean")
        if row.get("context_status") not in {"complete", "empty"}:
            raise RigInputError(f"{name}.context_status is not successful")
        if row.get("stack_fingerprint") != stack_digest:
            raise RigInputError(f"{name} has a different stack identity")
        _validate_provider_usage(row.get("provider_usage"), name=name)
        _validate_activity(row.get("activity"), mode=mode, name=name)
        validated_rows.append(dict(row))
    if {domain for domain, _question_id in seen} != DOMAINS:
        raise RigInputError(f"{side} rows do not cover both Small domains")
    return {
        **raw,
        "name": arm_name,
        "configuration": dict(configuration),
        "geometry": dict(geometry),
        "rows": validated_rows,
    }


def validate_pass(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != RUN_PAIR_SCHEMA_VERSION:
        raise RigInputError("paired pass schema is invalid")
    _nonempty_string(raw.get("pass_id"), name="pass_id")
    _nonempty_string(raw.get("seed"), name="seed")
    stack = validate_stack(raw.get("stack"))
    digest = stack_fingerprint(stack)
    arms = raw.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"left", "right"}:
        raise RigInputError("paired pass must contain left and right arms")
    left = validate_arm(arms["left"], stack_digest=digest, side="left")
    right = validate_arm(arms["right"], stack_digest=digest, side="right")
    left_ids = {(row["domain"], row["question_id"]) for row in left["rows"]}
    right_ids = {(row["domain"], row["question_id"]) for row in right["rows"]}
    if left_ids != right_ids:
        raise RigInputError("paired arms do not contain the same question rows")
    return {**raw, "stack": stack, "arms": {"left": left, "right": right}}


def arm_summary(arm: dict[str, Any]) -> dict[str, Any]:
    rows = arm["rows"]
    scores = [float(row["score_bool"]) for row in rows]
    latencies = [float(row["latency_seconds"]) for row in rows]
    tokens = [float(row["reader_tokens"]) for row in rows]
    exposure = [float(row["evidence_exposed"]) for row in rows]
    accuracy_by_domain = {
        domain: _mean([float(row["score_bool"]) for row in rows if row["domain"] == domain])
        for domain in sorted(DOMAINS)
    }
    exposure_by_domain = {
        domain: _mean([float(row["evidence_exposed"]) for row in rows if row["domain"] == domain])
        for domain in sorted(DOMAINS)
    }
    return {
        "name": arm["name"],
        "question_count": len(rows),
        "accuracy": _mean(scores),
        "accuracy_by_domain": accuracy_by_domain,
        "evidence_exposure": _mean(exposure),
        "evidence_exposure_by_domain": exposure_by_domain,
        "latency_mean_seconds": _mean(latencies),
        "latency_p95_seconds": _percentile(latencies, 0.95),
        "reader_tokens_mean": _mean(tokens),
        "reader_tokens_total": int(sum(tokens)),
        "provider_requests": sum(int(row["provider_usage"]["requests"]) for row in rows),
        "activity_events": sum(int(row["activity"]["activity_events"]) for row in rows),
    }


def _validate_pass_set(passes: list[dict[str, Any]], *, expected_count: int) -> None:
    if len(passes) != expected_count:
        raise RigInputError(f"expected {expected_count} paired passes, received {len(passes)}")
    if len({item["pass_id"] for item in passes}) != len(passes):
        raise RigInputError("paired pass IDs are not unique")
    if len({item["seed"] for item in passes}) != len(passes):
        raise RigInputError("paired pass seeds are not unique")
    first_stack = passes[0]["stack"]
    if any(item["stack"] != first_stack for item in passes[1:]):
        raise RigInputError("paired passes changed stack, model, or dataset identity")


def _aa_outcome(absolute_deltas: list[float]) -> tuple[str, bool]:
    observed_span = max(absolute_deltas)
    first_three_span = max(absolute_deltas[:INITIAL_AA_PASS_COUNT])
    if len(absolute_deltas) == INITIAL_AA_PASS_COUNT and observed_span > INITIAL_NOISE_FLOOR_PP:
        return "NEEDS_TWO_MORE", False
    if len(absolute_deltas) == EXTENDED_AA_PASS_COUNT and any(
        delta > first_three_span for delta in absolute_deltas[INITIAL_AA_PASS_COUNT:]
    ):
        return "RIG_BLOCKED", False
    return "PASS", True


def build_aa_receipt(raw_passes: list[dict[str, Any]]) -> dict[str, Any]:
    passes = [validate_pass(item) for item in raw_passes]
    if len(passes) not in {INITIAL_AA_PASS_COUNT, EXTENDED_AA_PASS_COUNT}:
        raise RigInputError("A/A requires exactly three or five paired passes")
    _validate_pass_set(passes, expected_count=len(passes))
    first_arm = passes[0]["arms"]["left"]
    arm_contract = {
        "name": first_arm["name"],
        "configuration": first_arm["configuration"],
        "geometry": first_arm["geometry"],
    }
    pass_rows: list[dict[str, Any]] = []
    for item in passes:
        left = item["arms"]["left"]
        right = item["arms"]["right"]
        if left["name"] != right["name"]:
            raise RigInputError("A/A arm names differ")
        if left["configuration"] != right["configuration"]:
            raise RigInputError("A/A arm configurations differ")
        if left["geometry"] != right["geometry"]:
            raise RigInputError("A/A arm geometry differs")
        current_contract = {
            "name": left["name"],
            "configuration": left["configuration"],
            "geometry": left["geometry"],
        }
        if current_contract != arm_contract:
            raise RigInputError("A/A arm configuration or geometry changed between passes")
        left_summary = arm_summary(left)
        right_summary = arm_summary(right)
        pass_rows.append(
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "left": left_summary,
                "right": right_summary,
                "accuracy_delta_pp": 100 * (right_summary["accuracy"] - left_summary["accuracy"]),
                "latency_delta_seconds": (
                    right_summary["latency_mean_seconds"] - left_summary["latency_mean_seconds"]
                ),
                "reader_token_delta": (
                    right_summary["reader_tokens_mean"] - left_summary["reader_tokens_mean"]
                ),
            }
        )
    absolute_deltas = [abs(float(item["accuracy_delta_pp"])) for item in pass_rows]
    observed_span = max(absolute_deltas)
    first_three_span = max(absolute_deltas[:INITIAL_AA_PASS_COUNT])
    status, stable = _aa_outcome(absolute_deltas)
    payload = {
        "schema_version": AA_SCHEMA_VERSION,
        "status": status,
        "paid_benchmark_allowed": status == "PASS",
        "score_claim_allowed": False,
        "stack": passes[0]["stack"],
        "arm_contract": arm_contract,
        "pass_count": len(passes),
        "passes": pass_rows,
        "observed_span_pp": observed_span,
        "first_three_span_pp": first_three_span,
        "stabilized": stable,
        "noise_floor_pp": max(INITIAL_NOISE_FLOOR_PP, observed_span),
        "stabilization_rule": (
            "three passes pass at or below 3pp; above 3pp requires two more passes, "
            "neither of which may expand the first-three span"
        ),
    }
    payload["aa_receipt_sha256"] = canonical_sha256(payload)
    return payload


def _aa_receipt_deltas(raw: dict[str, Any]) -> list[float]:
    pass_count = _positive_int(raw.get("pass_count"), name="A/A pass_count")
    pass_rows = raw.get("passes")
    if pass_count not in {INITIAL_AA_PASS_COUNT, EXTENDED_AA_PASS_COUNT}:
        raise RigInputError("A/A receipt requires exactly three or five passes")
    if not isinstance(pass_rows, list) or len(pass_rows) != pass_count:
        raise RigInputError("A/A receipt pass count does not match its rows")
    pass_ids: set[str] = set()
    seeds: set[str] = set()
    absolute_deltas: list[float] = []
    for index, row in enumerate(pass_rows):
        if not isinstance(row, dict):
            raise RigInputError(f"A/A passes[{index}] is not an object")
        pass_id = _nonempty_string(row.get("pass_id"), name=f"A/A passes[{index}].pass_id")
        seed = _nonempty_string(row.get("seed"), name=f"A/A passes[{index}].seed")
        if pass_id in pass_ids or seed in seeds:
            raise RigInputError("A/A receipt pass IDs and seeds must be unique")
        pass_ids.add(pass_id)
        seeds.add(seed)
        delta = _finite_number(
            row.get("accuracy_delta_pp"),
            name=f"A/A passes[{index}].accuracy_delta_pp",
        )
        absolute_deltas.append(abs(delta))
    return absolute_deltas


def _validate_aa_metrics(raw: dict[str, Any], absolute_deltas: list[float]) -> None:
    observed_span = max(absolute_deltas)
    first_three_span = max(absolute_deltas[:INITIAL_AA_PASS_COUNT])
    expected_status, expected_stable = _aa_outcome(absolute_deltas)
    noise_floor = max(INITIAL_NOISE_FLOOR_PP, observed_span)
    if raw.get("status") != expected_status or raw.get("stabilized") is not expected_stable:
        raise RigInputError("A/A receipt status does not match its pass deltas")
    if raw.get("paid_benchmark_allowed") is not (expected_status == "PASS"):
        raise RigInputError("A/A paid-work verdict does not match its status")
    if raw.get("score_claim_allowed") is not False:
        raise RigInputError("A/A receipt cannot authorize a score claim")
    for field, expected in (
        ("observed_span_pp", observed_span),
        ("first_three_span_pp", first_three_span),
        ("noise_floor_pp", noise_floor),
    ):
        actual = _finite_number(raw.get(field), name=f"A/A {field}", minimum=0)
        if not math.isclose(actual, expected, abs_tol=1e-9):
            raise RigInputError(f"A/A {field} does not match its pass deltas")


def _validate_aa_arm_contract(raw: dict[str, Any]) -> None:
    arm_contract = raw.get("arm_contract")
    if not isinstance(arm_contract, dict):
        raise RigInputError("A/A arm contract is missing")
    _nonempty_string(arm_contract.get("name"), name="A/A arm name")
    configuration = arm_contract.get("configuration")
    geometry = arm_contract.get("geometry")
    if not isinstance(configuration, dict) or not configuration:
        raise RigInputError("A/A arm configuration is missing")
    _nonempty_string(configuration.get("retrieval_mode"), name="A/A retrieval mode")
    if not isinstance(geometry, dict):
        raise RigInputError("A/A arm geometry is missing")
    _positive_int(geometry.get("max_context_items"), name="A/A max_context_items")
    _positive_int(
        geometry.get("max_context_total_chars"),
        name="A/A max_context_total_chars",
    )


def validate_aa_receipt(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != AA_SCHEMA_VERSION:
        raise RigInputError("A/A receipt is missing or invalid")
    digest = raw.get("aa_receipt_sha256")
    unsigned = {key: value for key, value in raw.items() if key != "aa_receipt_sha256"}
    if digest != canonical_sha256(unsigned):
        raise RigInputError("A/A receipt digest does not bind its content")
    stack = validate_stack(raw.get("stack"))
    absolute_deltas = _aa_receipt_deltas(raw)
    _validate_aa_metrics(raw, absolute_deltas)
    _validate_aa_arm_contract(raw)
    return {**raw, "stack": stack}


def _require_passing_aa_receipt(raw: object) -> dict[str, Any]:
    receipt = validate_aa_receipt(raw)
    if receipt["status"] != "PASS" or receipt["paid_benchmark_allowed"] is not True:
        raise RigInputError("paid benchmark work requires a stabilized PASS A/A receipt")
    return receipt


def build_anchor_receipt(
    raw_pass: dict[str, Any],
    *,
    arm_side: str,
    aa_receipt: dict[str, Any],
) -> dict[str, Any]:
    paired_pass = validate_pass(raw_pass)
    validated_aa = _require_passing_aa_receipt(aa_receipt)
    if paired_pass["stack"] != validated_aa["stack"]:
        raise RigInputError("anchor stack does not match its A/A receipt")
    if arm_side not in {"left", "right"}:
        raise RigInputError("anchor arm side must be left or right")
    arm = paired_pass["arms"][arm_side]
    arm_contract = {
        "name": arm["name"],
        "configuration": arm["configuration"],
        "geometry": arm["geometry"],
    }
    if arm_contract != validated_aa["arm_contract"]:
        raise RigInputError("anchor arm configuration or geometry differs from A/A")
    summary = arm_summary(arm)
    lafs = _finite_number(arm.get("official_lafs"), name="anchor official_lafs")
    noise_floor = float(validated_aa["noise_floor_pp"])
    if lafs <= 0:
        raise RigInputError("anchor cannot support a claim without positive official LAFS")
    return {
        "schema_version": ANCHOR_SCHEMA_VERSION,
        "status": "PASS",
        "claim_allowed": True,
        "historical_denominator_allowed": False,
        "stack": paired_pass["stack"],
        "aa_receipt_sha256": validated_aa["aa_receipt_sha256"],
        "arm": arm_contract,
        "metrics": {**summary, "official_lafs": lafs, "noise_floor_pp": noise_floor},
    }


def freeze_preregistration(raw: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if kind not in {"race", "render"}:
        raise RigInputError("preregistration kind must be race or render")
    forbidden = {"accuracy", "score", "score_bool", "results", "metrics"}

    def reject_scores(value: object, path: str = "input") -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).casefold() in forbidden:
                    raise RigInputError(f"{path}.{key} is score-bearing")
                reject_scores(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                reject_scores(nested, f"{path}[{index}]")

    if {"aa_span_pp", "noise_floor_pp", "aa_receipt_sha256"}.intersection(raw):
        raise RigInputError("A/A thresholds are derived from the bound receipt")
    aa_receipt = _require_passing_aa_receipt(raw.get("aa_receipt"))
    reject_scores({key: value for key, value in raw.items() if key != "aa_receipt"})
    stack = validate_stack(raw.get("stack"))
    if stack != aa_receipt["stack"]:
        raise RigInputError("preregistration stack does not match its A/A receipt")
    seeds = raw.get("seeds")
    if not isinstance(seeds, list) or len(seeds) != PAIRED_PASS_COUNT:
        raise RigInputError("preregistration must freeze exactly three seeds")
    normalized_seeds = [_nonempty_string(seed, name="seed") for seed in seeds]
    if len(set(normalized_seeds)) != PAIRED_PASS_COUNT:
        raise RigInputError("preregistered seeds must be unique")
    expected_rule = RACE_DECISION_RULE if kind == "race" else RENDER_DECISION_RULE
    if raw.get("decision_rule") != expected_rule:
        raise RigInputError(f"{kind} decision rule does not match the v1.3 contract")
    required = (
        {"machine_configuration", "naive_configuration", "shipping_geometry", "matched_geometry"}
        if kind == "race"
        else {
            "control_configuration",
            "treatment_configuration",
            "geometry",
            "included_levers",
            "replay_survivors",
        }
    )
    missing = sorted(key for key in required if key not in raw)
    if missing:
        raise RigInputError(f"{kind} preregistration is missing {missing}")
    payload = {
        **raw,
        "schema_version": PREREGISTRATION_SCHEMA_VERSION,
        "kind": kind,
        "stack": stack,
        "seeds": normalized_seeds,
        "aa_receipt": aa_receipt,
        "aa_receipt_sha256": aa_receipt["aa_receipt_sha256"],
        "aa_span_pp": aa_receipt["observed_span_pp"],
        "noise_floor_pp": aa_receipt["noise_floor_pp"],
    }
    payload["preregistration_sha256"] = canonical_sha256(payload)
    return payload


def validate_preregistration(raw: dict[str, Any], *, kind: str) -> dict[str, Any]:
    digest = raw.get("preregistration_sha256")
    unsigned = {key: value for key, value in raw.items() if key != "preregistration_sha256"}
    if digest != canonical_sha256(unsigned):
        raise RigInputError("preregistration digest does not bind its content")
    refrozen = freeze_preregistration(
        {
            key: value
            for key, value in unsigned.items()
            if key
            not in {
                "schema_version",
                "kind",
                "aa_receipt_sha256",
                "aa_span_pp",
                "noise_floor_pp",
            }
        },
        kind=kind,
    )
    if refrozen != raw:
        raise RigInputError("preregistration content is not canonical")
    return raw


def _arm_by_name(paired_pass: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [arm for arm in paired_pass["arms"].values() if arm["name"] == name]
    if len(matches) != 1:
        raise RigInputError(f"paired pass must contain exactly one {name!r} arm")
    return matches[0]


def _require_preregistered_passes(
    preregistration: dict[str, Any],
    passes: list[dict[str, Any]],
) -> None:
    _validate_pass_set(passes, expected_count=PAIRED_PASS_COUNT)
    if passes[0]["stack"] != preregistration["stack"]:
        raise RigInputError("paired passes do not match the preregistered stack")
    expected_seeds = list(preregistration["seeds"])
    if [item["seed"] for item in passes] != expected_seeds:
        raise RigInputError("paired pass seeds do not match preregistration order")
    digest = preregistration["preregistration_sha256"]
    if any(item.get("preregistration_sha256") != digest for item in passes):
        raise RigInputError("paired pass is not bound to the preregistration")


def build_race_receipt(
    raw_preregistration: dict[str, Any],
    raw_passes: list[dict[str, Any]],
    raw_matched_control: dict[str, Any],
) -> dict[str, Any]:
    preregistration = validate_preregistration(raw_preregistration, kind="race")
    passes = [validate_pass(item) for item in raw_passes]
    _require_preregistered_passes(preregistration, passes)
    noise_floor = float(preregistration["noise_floor_pp"])
    pass_rows: list[dict[str, Any]] = []
    for item in passes:
        machine = _arm_by_name(item, "machine")
        naive = _arm_by_name(item, "naive")
        if machine["configuration"] != preregistration["machine_configuration"]:
            raise RigInputError("machine configuration differs from preregistration")
        if naive["configuration"] != preregistration["naive_configuration"]:
            raise RigInputError("naive configuration differs from preregistration")
        shipping = preregistration["shipping_geometry"]
        if machine["geometry"] != shipping.get("machine") or naive["geometry"] != shipping.get(
            "naive"
        ):
            raise RigInputError("shipping geometry differs from preregistration")
        machine_summary = arm_summary(machine)
        naive_summary = arm_summary(naive)
        pass_rows.append(
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "machine": machine_summary,
                "naive": naive_summary,
                "accuracy_delta_pp": 100
                * (machine_summary["accuracy"] - naive_summary["accuracy"]),
                "naive_to_machine_latency_ratio": (
                    naive_summary["latency_mean_seconds"] / machine_summary["latency_mean_seconds"]
                    if machine_summary["latency_mean_seconds"] > 0
                    else math.inf
                ),
            }
        )
    matched = validate_pass(raw_matched_control)
    if matched["stack"] != preregistration["stack"]:
        raise RigInputError("matched-character control changed the stack")
    if matched.get("preregistration_sha256") != preregistration["preregistration_sha256"]:
        raise RigInputError("matched-character control is not preregistration-bound")
    matched_machine = _arm_by_name(matched, "machine")
    matched_naive = _arm_by_name(matched, "naive")
    if (
        matched_machine["geometry"] != matched_naive["geometry"]
        or matched_machine["geometry"] != preregistration["matched_geometry"]
    ):
        raise RigInputError("matched-character control geometry is not matched")
    deltas = [float(item["accuracy_delta_pp"]) for item in pass_rows]
    latency_ratios = [float(item["naive_to_machine_latency_ratio"]) for item in pass_rows]
    mean_delta = _mean(deltas)
    if mean_delta >= noise_floor and all(delta > 0 for delta in deltas):
        decision = "RETAIN_MACHINE_ON_ACCURACY"
    elif (
        mean_delta < noise_floor
        and all(delta < noise_floor for delta in deltas)
        and all(ratio <= NAIVE_MAXIMUM_LATENCY_RATIO for ratio in latency_ratios)
    ):
        decision = "V1_4_MACHINE_DELETION_CANDIDATE"
    else:
        decision = "INCONCLUSIVE_KEEP_MACHINE_DEFAULT"
    return {
        "schema_version": RACE_SCHEMA_VERSION,
        "status": "PASS",
        "decision": decision,
        "machine_deleted": False,
        "naive_default": False,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "stack": preregistration["stack"],
        "noise_floor_pp": noise_floor,
        "aa_span_pp": preregistration["aa_span_pp"],
        "mean_accuracy_delta_pp": mean_delta,
        "every_pass_positive": all(delta > 0 for delta in deltas),
        "passes": pass_rows,
        "matched_character_control": {
            "pass_id": matched["pass_id"],
            "machine": arm_summary(matched_machine),
            "naive": arm_summary(matched_naive),
        },
        "decision_rule": RACE_DECISION_RULE,
    }


def build_render_receipt(
    raw_preregistration: dict[str, Any],
    raw_passes: list[dict[str, Any]],
) -> dict[str, Any]:
    preregistration = validate_preregistration(raw_preregistration, kind="render")
    passes = [validate_pass(item) for item in raw_passes]
    _require_preregistered_passes(preregistration, passes)
    included_levers = preregistration["included_levers"]
    replay_survivors = preregistration["replay_survivors"]
    if (
        not isinstance(included_levers, list)
        or not included_levers
        or not isinstance(replay_survivors, dict)
        or any(replay_survivors.get(lever) is not True for lever in included_levers)
    ):
        raise RigInputError("render bundle contains a lever that did not survive replay")
    pass_rows: list[dict[str, Any]] = []
    for item in passes:
        control = _arm_by_name(item, "render_control")
        treatment = _arm_by_name(item, "render_treatment")
        if control["configuration"] != preregistration["control_configuration"]:
            raise RigInputError("render control differs from preregistration")
        if treatment["configuration"] != preregistration["treatment_configuration"]:
            raise RigInputError("render treatment differs from preregistration")
        if (
            control["geometry"] != preregistration["geometry"]
            or treatment["geometry"] != preregistration["geometry"]
        ):
            raise RigInputError("render bundle changed its hard geometry ceiling")
        lever_activity = treatment.get("lever_activity")
        if not isinstance(lever_activity, dict) or any(
            not isinstance(lever_activity.get(lever), int) or lever_activity[lever] <= 0
            for lever in included_levers
        ):
            raise RigInputError("render treatment has an included lever with no activity")
        control_summary = arm_summary(control)
        treatment_summary = arm_summary(treatment)
        pass_rows.append(
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "control": control_summary,
                "treatment": treatment_summary,
                "accuracy_delta_pp": 100
                * (treatment_summary["accuracy"] - control_summary["accuracy"]),
                "latency_regression_seconds": (
                    treatment_summary["latency_mean_seconds"]
                    - control_summary["latency_mean_seconds"]
                ),
                "reader_token_regression_ratio": (
                    treatment_summary["reader_tokens_mean"] / control_summary["reader_tokens_mean"]
                    - 1
                ),
                "exposure_delta_pp_by_domain": {
                    domain: 100
                    * (
                        treatment_summary["evidence_exposure_by_domain"][domain]
                        - control_summary["evidence_exposure_by_domain"][domain]
                    )
                    for domain in sorted(DOMAINS)
                },
                "lever_activity": dict(lever_activity),
            }
        )
    accuracy_deltas = [float(item["accuracy_delta_pp"]) for item in pass_rows]
    latency_regressions = [float(item["latency_regression_seconds"]) for item in pass_rows]
    token_regressions = [float(item["reader_token_regression_ratio"]) for item in pass_rows]
    exposure_gain_by_domain = {
        domain: _mean([float(item["exposure_delta_pp_by_domain"][domain]) for item in pass_rows])
        for domain in sorted(DOMAINS)
    }
    noise_floor = float(preregistration["noise_floor_pp"])
    exposure_gate = all(
        value >= RENDER_MINIMUM_EXPOSURE_GAIN_PP for value in exposure_gain_by_domain.values()
    )
    accuracy_gate = _mean(accuracy_deltas) > noise_floor and all(
        delta > 0 for delta in accuracy_deltas
    )
    latency_gate = _mean(latency_regressions) <= RENDER_MAXIMUM_LATENCY_REGRESSION_SECONDS
    token_gate = _mean(token_regressions) <= RENDER_MAXIMUM_TOKEN_REGRESSION_RATIO
    if exposure_gate and accuracy_gate and latency_gate and token_gate:
        decision = "SHIP_RENDER_BUNDLE"
    elif not exposure_gate or _mean(accuracy_deltas) <= 0:
        decision = "KILL_RENDER_BUNDLE"
    else:
        decision = "INCONCLUSIVE_DO_NOT_SHIP"
    return {
        "schema_version": RENDER_SCHEMA_VERSION,
        "status": "PASS",
        "decision": decision,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "stack": preregistration["stack"],
        "included_levers": included_levers,
        "noise_floor_pp": noise_floor,
        "mean_accuracy_delta_pp": _mean(accuracy_deltas),
        "mean_latency_regression_seconds": _mean(latency_regressions),
        "mean_reader_token_regression_ratio": _mean(token_regressions),
        "exposure_gain_pp_by_domain": exposure_gain_by_domain,
        "gates": {
            "exposure": exposure_gate,
            "accuracy": accuracy_gate,
            "latency": latency_gate,
            "reader_tokens": token_gate,
        },
        "passes": pass_rows,
        "decision_rule": RENDER_DECISION_RULE,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preregister = subparsers.add_parser("preregister")
    preregister.add_argument("--kind", choices=("race", "render"), required=True)
    preregister.add_argument("--input", required=True)
    preregister.add_argument("--output", required=True)

    aa = subparsers.add_parser("aa")
    aa.add_argument("--pass", dest="passes", action="append", required=True)
    aa.add_argument("--output", required=True)

    anchor = subparsers.add_parser("anchor")
    anchor.add_argument("--pass", dest="paired_pass", required=True)
    anchor.add_argument("--arm-side", choices=("left", "right"), required=True)
    anchor.add_argument("--aa-receipt", required=True)
    anchor.add_argument("--output", required=True)

    race = subparsers.add_parser("race")
    race.add_argument("--preregistration", required=True)
    race.add_argument("--pass", dest="passes", action="append", required=True)
    race.add_argument("--matched-control", required=True)
    race.add_argument("--output", required=True)

    render = subparsers.add_parser("render")
    render.add_argument("--preregistration", required=True)
    render.add_argument("--pass", dest="passes", action="append", required=True)
    render.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = Path(args.output)
    try:
        if args.command == "preregister":
            payload = freeze_preregistration(load_json(Path(args.input)), kind=args.kind)
        elif args.command == "aa":
            payload = build_aa_receipt([load_json(Path(path)) for path in args.passes])
        elif args.command == "anchor":
            payload = build_anchor_receipt(
                load_json(Path(args.paired_pass)),
                arm_side=args.arm_side,
                aa_receipt=load_json(Path(args.aa_receipt)),
            )
        elif args.command == "race":
            payload = build_race_receipt(
                load_json(Path(args.preregistration)),
                [load_json(Path(path)) for path in args.passes],
                load_json(Path(args.matched_control)),
            )
        elif args.command == "render":
            payload = build_render_receipt(
                load_json(Path(args.preregistration)),
                [load_json(Path(path)) for path in args.passes],
            )
        else:
            raise RuntimeError(f"unknown command {args.command!r}")
    except (OSError, json.JSONDecodeError, RigInputError) as exc:
        payload = {
            "schema_version": FAILURE_SCHEMA_VERSION,
            "status": "FAIL",
            "score_claim_allowed": False,
            "paid_benchmark_allowed": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        write_json(output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))  # noqa: T201
        return 1
    write_json(output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))  # noqa: T201
    if payload.get("status") == "RIG_BLOCKED":
        return 1
    if payload.get("status") == "NEEDS_TWO_MORE":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
