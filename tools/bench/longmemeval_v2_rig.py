#!/usr/bin/env python3
"""Generate fail-closed Sibyl v1.3 LongMemEval-V2 decision receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.longmemeval_v2_official_source import (  # noqa: E402
    OFFICIAL_HARNESS_COMMIT,
    OFFICIAL_HARNESS_DIFF_URL,
    OFFICIAL_HARNESS_PATH,
    OFFICIAL_HARNESS_PREVIOUS_COMMIT,
    OFFICIAL_REPO_URL,
)

ARM_RUN_SCHEMA_VERSION = "sibyl-longmemeval-v2-arm-run-v2"
RUN_PAIR_SCHEMA_VERSION = "sibyl-longmemeval-v2-paired-pass-v3"
EXECUTION_IDENTITY_SCHEMA_VERSION = "sibyl-longmemeval-v2-execution-identity-v1"
PREREGISTRATION_SCHEMA_VERSION = "sibyl-longmemeval-v2-preregistration-v2"
AA_SCHEMA_VERSION = "sibyl-longmemeval-v2-aa-receipt-v3"
ANCHOR_SCHEMA_VERSION = "sibyl-longmemeval-v2-anchor-receipt-v2"
RACE_SCHEMA_VERSION = "sibyl-longmemeval-v2-machine-race-receipt-v2"
RENDER_SCHEMA_VERSION = "sibyl-longmemeval-v2-render-receipt-v2"
FAILURE_SCHEMA_VERSION = "sibyl-longmemeval-v2-rig-failure-v1"
RIG_BLOCKED_SCHEMA_VERSION = "sibyl-longmemeval-v2-rig-blocked-receipt-v1"
DISPATCH_LEDGER_SCHEMA_VERSION = "sibyl-longmemeval-v2-dispatch-ledger-v1"
# The two ways the rig can block. A span-unstable block carries five completed
# paired passes; a dispatch-exhausted block carries five controller dispatches
# that never produced a single completed two-domain pass. They are different
# evidence and must never be read as the same receipt.
BLOCKED_REASON_SPAN_UNSTABLE = "span_unstable"
BLOCKED_REASON_DISPATCH_EXHAUSTED = "dispatch_exhausted"
CONTROLLER_WORKFLOW_NAME = "LongMemEval V2 Release A/A"
CONTROLLER_WORKFLOW_PATH = ".github/workflows/longmemeval-v2-release-aa.yml"
BUILDER_WORKFLOW_NAME = "LongMemEval V2"
BUILDER_WORKFLOW_PATH = ".github/workflows/longmemeval-v2.yml"
DISPATCH_BUILDER_ARM_ID = "aa-1-left"
# Dispatch evidence is trusted only from the release repository and branch. A
# ledger that names anything else is rejected before any row is read, so a
# self-consistent foreign ledger cannot seal a release verdict.
TRUSTED_REPOSITORY = "hyperb1iss/sibyl"
TRUSTED_BRANCH = "main"
LEDGER_PROVENANCE_VERIFIED = "github_verified"
LEDGER_PROVENANCE_UNVERIFIED = "unverified"
GITHUB_RUN_ENDPOINT = "repos/{repository}/actions/runs/{run_id}"
GITHUB_JOBS_ENDPOINT = "repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"
GITHUB_BUILDER_RUNS_ENDPOINT = (
    "repos/{repository}/actions/workflows/{workflow}/runs"
    "?event=workflow_dispatch&per_page=100&created=>={date}"
)
OFFICIAL_JOB_NAMES = {
    "enterprise": "Official enterprise small",
    "web": "Official web small",
    "combined_receipt": "Official combined receipt",
}
DISPATCH_EXHAUSTION_RULE = (
    "at least five successful controller dispatches on one branch, each binding "
    "a builder run whose official Small domains never both succeeded and whose "
    "combined receipt never succeeded"
)
# GitHub stamps a skipped job (one that never ran) with bookkeeping times that can
# run backwards. Every skipped job in the observed r1-r5 ledger shows a reversal
# of zero or one second, and both timestamps are second-granular, so two seconds
# allows one rounding tick on each side and nothing more.
SKIPPED_JOB_MAX_CLOCK_SKEW_SECONDS = 2
COMPLETED_RUN_CONCLUSIONS = frozenset({"success", "failure", "cancelled"})
COMPLETED_JOB_CONCLUSIONS = frozenset({"success", "failure", "cancelled", "skipped"})
EXPERIMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
DOMAINS = frozenset({"web", "enterprise"})
OFFICIAL_SMALL_QUESTION_COUNTS = {
    "enterprise": 211,
    "web": 240,
}
OFFICIAL_SMALL_QUESTION_IDS_SHA256 = {
    "enterprise": "sha256:984368308cc83c63401bf5e3d53d33a635b2768d434215a52cc9d5effee66c19",
    "web": "sha256:bb4183ef7f554ef278b158b910c6e8c1de6d14572dae8d29789dded57a143eeb",
}
EXPERIMENT_PHASES = frozenset({"aa", "anchor", "race", "render"})
INITIAL_NOISE_FLOOR_PP = 3.0
ASCII_DELETE_CODEPOINT = 127
GIT_REF_FORBIDDEN_ASCII_MAX = 32
GIT_SHA_LENGTH = 40
GITHUB_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}"
)
POSITIVE_DECIMAL_PATTERN = re.compile(r"[1-9][0-9]*")
WORKFLOW_FILENAME_PATTERN = re.compile(r"[^/@\x00-\x20\x7f]+\.ya?ml")
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

AA_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "blocked_reason",
        "paid_benchmark_allowed",
        "score_claim_allowed",
        "stack",
        "arm_contract",
        "pass_count",
        "passes",
        "observed_span_pp",
        "first_three_span_pp",
        "stabilized",
        "noise_floor_pp",
        "stabilization_rule",
        "aa_receipt_sha256",
    }
)
ARM_RUN_KEYS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "experiment_phase",
        "pass_id",
        "seed",
        "name",
        "substrate",
        "preregistration_sha256",
        "execution",
        "stack",
        "configuration",
        "geometry",
        "rows",
        "provider_usage",
        "lever_activity",
        "source_artifacts",
        "official_question_count_by_domain",
        "official_question_ids_sha256_by_domain",
        "question_order_sha256",
        "arm_run_sha256",
    }
)
PAIRED_PASS_KEYS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "experiment_phase",
        "pass_id",
        "seed",
        "stack",
        "preregistration_sha256",
        "arms",
        "paired_pass_sha256",
    }
)
ARM_ROW_KEYS = frozenset(
    {
        "status",
        "domain",
        "question_id",
        "score_bool",
        "latency_seconds",
        "reader_tokens",
        "evidence_exposure_eligible",
        "evidence_exposed",
        "context_status",
        "stack_fingerprint",
        "activity",
    }
)
EXECUTION_COMMON_KEYS = frozenset(
    {"schema_version", "kind", "repository", "ref", "sha", "run_id", "run_attempt"}
)
GITHUB_EXECUTION_KEYS = frozenset(EXECUTION_COMMON_KEYS | {"workflow_ref"})
LOCAL_EXECUTION_KEYS = EXECUTION_COMMON_KEYS
PROVIDER_USAGE_KEYS = frozenset(
    {
        "complete",
        "requests",
        "total_tokens",
        "actual_cost_usd",
        "max_spend_usd_total",
        "run_ids_by_domain",
    }
)
ANCHOR_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "anchor_publishable",
        "comparative_claim_allowed",
        "historical_denominator_allowed",
        "stack",
        "aa_receipt_sha256",
        "aa_pass_count",
        "aa_observed_span_pp",
        "arm_run_sha256",
        "arm",
        "metrics",
        "anchor_receipt_sha256",
    }
)
RACE_PASS_ROW_KEYS = frozenset(
    {
        "pass_id",
        "seed",
        "paired_pass_sha256",
        "machine",
        "naive",
        "accuracy_delta_pp",
        "naive_to_machine_latency_ratio",
    }
)
RACE_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "decision",
        "selected_render_substrate",
        "machine_deleted",
        "naive_default",
        "preregistration_sha256",
        "aa_receipt_sha256",
        "stack",
        "noise_floor_pp",
        "aa_span_pp",
        "mean_accuracy_delta_pp",
        "every_pass_positive",
        "passes",
        "matched_character_control",
        "decision_rule",
        "race_receipt_sha256",
    }
)
MATCHED_CONTROL_KEYS = frozenset({"pass_id", "seed", "paired_pass_sha256", "machine", "naive"})
RENDER_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "decision",
        "reason",
        "selected_render_substrate",
        "preregistration_sha256",
        "race_receipt_sha256",
        "stack",
        "included_levers",
        "noise_floor_pp",
        "mean_accuracy_delta_pp",
        "mean_latency_regression_seconds",
        "mean_reader_token_regression_ratio",
        "exposure_gain_pp_by_domain",
        "gates",
        "passes",
        "decision_rule",
        "render_receipt_sha256",
    }
)
ACTIVITY_KEYS = frozenset(
    {
        "retrieval_mode",
        "context_pack_requests",
        "activity_events",
        "naive_vector_attempts",
        "naive_vector_successes",
        "hybrid_vector_attempts",
        "hybrid_vector_successes",
        "planner_query_count",
        "typed_evidence_applicable",
        "typed_search_statuses",
        "mode",
        "lever_activity",
    }
)
GEOMETRY_KEYS = frozenset(
    {"max_context_items", "max_context_chars_per_item", "max_context_total_chars"}
)
STACK_KEYS = frozenset(
    {
        "sibyl_commit",
        "sibyl_git_status",
        "official_source",
        "dataset_sha256_by_domain",
        "reader",
        "judge",
    }
)
OFFICIAL_SOURCE_KEYS = frozenset(
    {
        "url",
        "path",
        "commit",
        "expected_commit",
        "pin_matches",
        "git_status",
        "harness_path",
        "harness_exists",
        "previous_reviewed_commit",
        "reviewed_diff_url",
    }
)
AA_PASS_ROW_KEYS = frozenset(
    {
        "pass_id",
        "seed",
        "paired_pass_sha256",
        "left",
        "right",
        "accuracy_delta_pp",
        "latency_delta_seconds",
        "reader_token_delta",
    }
)
ARM_SUMMARY_KEYS = frozenset(
    {
        "name",
        "question_count",
        "accuracy",
        "accuracy_by_domain",
        "evidence_exposure",
        "evidence_exposure_by_domain",
        "evidence_exposure_eligible_count",
        "evidence_exposure_eligible_count_by_domain",
        "latency_mean_seconds",
        "latency_p95_seconds",
        "reader_tokens_mean",
        "reader_tokens_total",
        "provider_requests",
        "activity_events",
    }
)


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


def _nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RigInputError(f"{name} must be a non-negative integer")
    return value


def _sha256_digest(value: object, *, name: str) -> str:
    digest = _nonempty_string(value, name=name)
    prefix = "sha256:"
    if (
        not digest.startswith(prefix)
        or len(digest) != len(prefix) + 64
        or any(character not in "0123456789abcdef" for character in digest[len(prefix) :])
    ):
        raise RigInputError(f"{name} must be a full lowercase SHA-256 digest")
    return digest


def _require_exact_keys(raw: dict[str, Any], expected: frozenset[str], *, name: str) -> None:
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise RigInputError(f"{name} fields differ: missing={missing}, unknown={unknown}")


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
    _require_exact_keys(raw, STACK_KEYS, name="stack")
    commit = _nonempty_string(raw.get("sibyl_commit"), name="stack.sibyl_commit")
    if len(commit) != GIT_SHA_LENGTH or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise RigInputError("stack.sibyl_commit must be a full lowercase Git SHA")
    if raw.get("sibyl_git_status") != "clean":
        raise RigInputError("benchmark decisions require a clean Sibyl checkout")
    official = raw.get("official_source")
    if not isinstance(official, dict):
        raise RigInputError("stack.official_source is missing")
    _require_exact_keys(official, OFFICIAL_SOURCE_KEYS, name="stack.official_source")
    if {
        "url": official.get("url"),
        "commit": official.get("commit"),
        "expected_commit": official.get("expected_commit"),
        "pin_matches": official.get("pin_matches"),
        "git_status": official.get("git_status"),
        "harness_path": official.get("harness_path"),
        "harness_exists": official.get("harness_exists"),
        "previous_reviewed_commit": official.get("previous_reviewed_commit"),
        "reviewed_diff_url": official.get("reviewed_diff_url"),
    } != {
        "url": OFFICIAL_REPO_URL,
        "commit": OFFICIAL_HARNESS_COMMIT,
        "expected_commit": OFFICIAL_HARNESS_COMMIT,
        "pin_matches": True,
        "git_status": "clean",
        "harness_path": OFFICIAL_HARNESS_PATH,
        "harness_exists": True,
        "previous_reviewed_commit": OFFICIAL_HARNESS_PREVIOUS_COMMIT,
        "reviewed_diff_url": OFFICIAL_HARNESS_DIFF_URL,
    }:
        raise RigInputError("stack.official_source is not the clean reviewed harness pin")
    _nonempty_string(official.get("path"), name="stack.official_source.path")
    dataset = raw.get("dataset_sha256_by_domain")
    if not isinstance(dataset, dict) or set(dataset) != DOMAINS:
        raise RigInputError("stack dataset hashes must cover both Small domains")
    for domain, digest in dataset.items():
        _sha256_digest(digest, name=f"stack dataset hash for {domain}")
    for role in ("reader", "judge"):
        model = raw.get(role)
        if not isinstance(model, dict) or not model:
            raise RigInputError(f"stack.{role} model identity is missing")
    return dict(raw)


def stack_fingerprint(stack: dict[str, Any]) -> str:
    return canonical_sha256(stack)


def _validate_geometry(raw: object, *, name: str) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is missing")
    _require_exact_keys(raw, GEOMETRY_KEYS, name=name)
    return {key: _positive_int(raw.get(key), name=f"{name}.{key}") for key in sorted(GEOMETRY_KEYS)}


def _is_valid_branch_ref(ref: str) -> bool:
    prefix = "refs/heads/"
    if not ref.startswith(prefix) or ref != ref.strip():
        return False
    branch = ref.removeprefix(prefix)
    if not branch or branch.startswith("-") or branch.endswith("."):
        return False
    if ".." in branch or "@{" in branch or "\\" in branch:
        return False
    if any(
        ord(character) <= GIT_REF_FORBIDDEN_ASCII_MAX or ord(character) == ASCII_DELETE_CODEPOINT
        for character in branch
    ):
        return False
    if any(character in "~^:?*[" for character in branch):
        return False
    components = branch.split("/")
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in components
    )


def _validate_workflow_ref(
    *,
    repository: str,
    ref: str,
    workflow_ref: str,
    name: str,
) -> None:
    prefix = f"{repository}/.github/workflows/"
    suffix = f"@{ref}"
    if not workflow_ref.startswith(prefix) or not workflow_ref.endswith(suffix):
        raise RigInputError(f"{name} is not canonical for its repository and ref")
    filename = workflow_ref[len(prefix) : -len(suffix)]
    if not WORKFLOW_FILENAME_PATTERN.fullmatch(filename):
        raise RigInputError(f"{name} must name one canonical YAML workflow file")
    if workflow_ref != f"{prefix}{filename}{suffix}":
        raise RigInputError(f"{name} is not canonical for its repository and ref")


def validate_execution_identity(raw: object, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is missing")
    kind = _nonempty_string(raw.get("kind"), name=f"{name}.kind")
    if kind not in {"github", "local"}:
        raise RigInputError(f"{name}.kind is invalid")
    _require_exact_keys(
        raw,
        GITHUB_EXECUTION_KEYS if kind == "github" else LOCAL_EXECUTION_KEYS,
        name=name,
    )
    if raw.get("schema_version") != EXECUTION_IDENTITY_SCHEMA_VERSION:
        raise RigInputError(f"{name}.schema_version is invalid")
    execution = dict(raw)
    repository = _nonempty_string(execution.get("repository"), name=f"{name}.repository")
    if not GITHUB_REPOSITORY_PATTERN.fullmatch(repository):
        raise RigInputError(f"{name}.repository must be a canonical owner/repository slug")
    ref = _nonempty_string(execution.get("ref"), name=f"{name}.ref")
    if not _is_valid_branch_ref(ref):
        raise RigInputError(f"{name}.ref must be a valid full refs/heads/* branch ref")
    sha = _nonempty_string(execution.get("sha"), name=f"{name}.sha")
    if len(sha) != GIT_SHA_LENGTH or any(character not in "0123456789abcdef" for character in sha):
        raise RigInputError(f"{name}.sha must be a full lowercase Git SHA")
    run_id = _nonempty_string(execution.get("run_id"), name=f"{name}.run_id")
    if run_id != execution["run_id"]:
        raise RigInputError(f"{name}.run_id must use its canonical value")
    if kind == "local":
        try:
            normalized_run_id = str(UUID(run_id))
        except ValueError as exc:
            raise RigInputError(f"{name}.run_id must be a canonical UUID") from exc
        if normalized_run_id != run_id:
            raise RigInputError(f"{name}.run_id must be a canonical UUID")
    else:
        if not POSITIVE_DECIMAL_PATTERN.fullmatch(run_id):
            raise RigInputError(f"{name}.run_id must be a canonical positive decimal string")
        workflow_ref = _nonempty_string(
            execution.get("workflow_ref"),
            name=f"{name}.workflow_ref",
        )
        _validate_workflow_ref(
            repository=repository,
            ref=ref,
            workflow_ref=workflow_ref,
            name=f"{name}.workflow_ref",
        )
    _positive_int(execution.get("run_attempt"), name=f"{name}.run_attempt")
    return execution


def _validate_provider_usage(raw: object, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("complete") is not True:
        raise RigInputError(f"{name} provider usage is incomplete")
    _require_exact_keys(raw, PROVIDER_USAGE_KEYS, name=f"{name}.provider_usage")
    _positive_int(raw.get("requests"), name=f"{name}.provider_usage.requests")
    _nonnegative_int(raw.get("total_tokens"), name=f"{name}.provider_usage.total_tokens")
    actual_cost = _finite_number(
        raw.get("actual_cost_usd"),
        name=f"{name}.provider_usage.actual_cost_usd",
        minimum=0,
    )
    maximum_cost = _finite_number(
        raw.get("max_spend_usd_total"),
        name=f"{name}.provider_usage.max_spend_usd_total",
        minimum=0,
    )
    if actual_cost > maximum_cost:
        raise RigInputError(f"{name}.provider_usage exceeds its approved spend")
    run_ids = raw.get("run_ids_by_domain")
    if not isinstance(run_ids, dict) or set(run_ids) != DOMAINS:
        raise RigInputError(f"{name}.provider_usage run IDs must cover both Small domains")
    for domain, run_id in run_ids.items():
        _nonempty_string(run_id, name=f"{name}.provider_usage.{domain}")
    return dict(raw)


def _validate_activity(raw: object, *, mode: str, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} has no arm activity receipt")
    _require_exact_keys(raw, ACTIVITY_KEYS, name=f"{name}.activity")
    _positive_int(raw.get("activity_events"), name=f"{name}.activity_events")
    if raw.get("mode") != mode or raw.get("retrieval_mode") != mode:
        raise RigInputError(f"{name} activity mode differs from its configuration")
    for field in (
        "context_pack_requests",
        "naive_vector_attempts",
        "naive_vector_successes",
        "hybrid_vector_attempts",
        "hybrid_vector_successes",
        "planner_query_count",
    ):
        _nonnegative_int(raw.get(field), name=f"{name}.{field}")
    for attempts_field, successes_field in (
        ("naive_vector_attempts", "naive_vector_successes"),
        ("hybrid_vector_attempts", "hybrid_vector_successes"),
    ):
        if raw[successes_field] > raw[attempts_field]:
            raise RigInputError(f"{name}.{successes_field} exceeds {attempts_field}")
    statuses = raw.get("typed_search_statuses")
    if not isinstance(statuses, list) or any(
        not isinstance(status, str) or not status for status in statuses
    ):
        raise RigInputError(f"{name}.typed_search_statuses is invalid")
    if mode == "naive":
        _positive_int(raw.get("naive_vector_attempts"), name=f"{name}.naive_vector_attempts")
        if raw.get("typed_evidence_applicable") is not False:
            raise RigInputError(f"{name} marks typed evidence applicable for naive retrieval")
    else:
        _positive_int(
            raw.get("hybrid_vector_attempts"),
            name=f"{name}.hybrid_vector_attempts",
        )
        if raw.get("typed_evidence_applicable") is not True:
            raise RigInputError(f"{name} does not mark typed evidence applicable")
    lever_activity = raw.get("lever_activity")
    if not isinstance(lever_activity, dict) or any(
        not isinstance(lever, str)
        or not lever
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count <= 0
        for lever, count in lever_activity.items()
    ):
        raise RigInputError(f"{name}.lever_activity is invalid")
    expected_activity_events = (
        raw["context_pack_requests"]
        + raw["naive_vector_attempts"]
        + raw["hybrid_vector_attempts"]
        + raw["planner_query_count"]
        + sum(lever_activity.values())
    )
    if raw["activity_events"] != expected_activity_events:
        raise RigInputError(f"{name}.activity_events does not match its explicit counters")
    return dict(raw)


def validate_arm(  # noqa: PLR0912, PLR0915
    raw: object,
    *,
    stack_digest: str,
    side: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{side} arm must be an object")
    if raw.get("schema_version") != ARM_RUN_SCHEMA_VERSION:
        raise RigInputError(f"{side} arm-run schema is invalid")
    _require_exact_keys(raw, ARM_RUN_KEYS, name=f"{side} arm run")
    arm_digest = raw.get("arm_run_sha256")
    unsigned = {key: value for key, value in raw.items() if key != "arm_run_sha256"}
    if arm_digest != canonical_sha256(unsigned):
        raise RigInputError(f"{side} arm-run digest does not bind its content")
    _nonempty_string(raw.get("experiment_id"), name=f"{side}.experiment_id")
    experiment_phase = _nonempty_string(
        raw.get("experiment_phase"),
        name=f"{side}.experiment_phase",
    )
    if experiment_phase not in EXPERIMENT_PHASES:
        raise RigInputError(f"{side}.experiment_phase is invalid")
    _nonempty_string(raw.get("pass_id"), name=f"{side}.pass_id")
    _nonnegative_int(raw.get("seed"), name=f"{side}.seed")
    arm_name = _nonempty_string(raw.get("name"), name=f"{side}.name")
    substrate = _nonempty_string(raw.get("substrate"), name=f"{side}.substrate")
    if substrate not in {"machine", "naive"}:
        raise RigInputError(f"{side}.substrate must be machine or naive")
    if experiment_phase in {"aa", "anchor"}:
        if raw.get("preregistration_sha256") != "":
            raise RigInputError(f"{side} {experiment_phase} arm cannot bind preregistration")
        preregistration_sha256 = ""
    else:
        preregistration_sha256 = _sha256_digest(
            raw.get("preregistration_sha256"),
            name=f"{side}.preregistration_sha256",
        )
    execution = validate_execution_identity(raw.get("execution"), name=f"{side}.execution")
    configuration = raw.get("configuration")
    if not isinstance(configuration, dict) or not configuration:
        raise RigInputError(f"{side}.configuration is missing")
    geometry = _validate_geometry(raw.get("geometry"), name=f"{side}.geometry")
    mode = _nonempty_string(configuration.get("retrieval_mode"), name=f"{side}.retrieval_mode")
    if substrate == "naive" and mode != "naive":
        raise RigInputError(f"{side} naive substrate does not use naive retrieval")
    if substrate == "machine" and mode == "naive":
        raise RigInputError(f"{side} machine substrate uses naive retrieval")
    arm_stack = validate_stack(raw.get("stack"))
    if execution["sha"] != arm_stack["sibyl_commit"]:
        raise RigInputError(f"{side} execution SHA does not match the arm stack")
    if stack_fingerprint(arm_stack) != stack_digest:
        raise RigInputError(f"{side} arm stack differs from the paired pass")
    provider_usage = _validate_provider_usage(raw.get("provider_usage"), name=side)
    lever_activity = raw.get("lever_activity")
    if not isinstance(lever_activity, dict) or any(
        not isinstance(lever, str)
        or not lever
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count <= 0
        for lever, count in lever_activity.items()
    ):
        raise RigInputError(f"{side}.lever_activity is invalid")
    source_artifacts = raw.get("source_artifacts")
    if not isinstance(source_artifacts, dict) or set(source_artifacts) != DOMAINS:
        raise RigInputError(f"{side}.source_artifacts must cover both Small domains")
    for domain, artifacts in source_artifacts.items():
        if not isinstance(artifacts, dict) or not artifacts:
            raise RigInputError(f"{side}.source_artifacts.{domain} is empty")
        for artifact_name, digest in artifacts.items():
            if not isinstance(artifact_name, str) or not artifact_name:
                raise RigInputError(f"{side}.source_artifacts.{domain} has an invalid name")
            _sha256_digest(
                digest,
                name=f"{side}.source_artifacts.{domain}.{artifact_name}",
            )
    official_question_counts = raw.get("official_question_count_by_domain")
    if not isinstance(official_question_counts, dict) or set(official_question_counts) != DOMAINS:
        raise RigInputError(f"{side}.official_question_count_by_domain is not exact")
    official_question_ids_sha256 = raw.get("official_question_ids_sha256_by_domain")
    if (
        not isinstance(official_question_ids_sha256, dict)
        or set(official_question_ids_sha256) != DOMAINS
    ):
        raise RigInputError(f"{side}.official_question_ids_sha256_by_domain is not exact")
    for domain in sorted(DOMAINS):
        count = _positive_int(
            official_question_counts[domain],
            name=f"{side}.official_question_count_by_domain.{domain}",
        )
        digest = _sha256_digest(
            official_question_ids_sha256[domain],
            name=f"{side}.official_question_ids_sha256_by_domain.{domain}",
        )
        if (
            count != OFFICIAL_SMALL_QUESTION_COUNTS[domain]
            or digest != OFFICIAL_SMALL_QUESTION_IDS_SHA256[domain]
        ):
            raise RigInputError(f"{side} {domain} corpus identity is not the pinned Small corpus")
    rows = raw.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RigInputError(f"{side}.rows is empty")
    seen: set[tuple[str, str]] = set()
    validated_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        name = f"{side}.rows[{index}]"
        if not isinstance(row, dict):
            raise RigInputError(f"{name} is not an object")
        _require_exact_keys(row, ARM_ROW_KEYS, name=name)
        if row.get("status") != "valid":
            raise RigInputError(f"{name} is failed or incomplete")
        domain = _nonempty_string(row.get("domain"), name=f"{name}.domain")
        if row.get("domain") != domain:
            raise RigInputError(f"{name}.domain must use its canonical value")
        if domain not in DOMAINS:
            raise RigInputError(f"{name}.domain is not a Small domain")
        question_id = _nonempty_string(row.get("question_id"), name=f"{name}.question_id")
        if row.get("question_id") != question_id:
            raise RigInputError(f"{name}.question_id must use its canonical value")
        identity = (domain, question_id)
        if identity in seen:
            raise RigInputError(f"{side} repeats row {identity}")
        seen.add(identity)
        if not isinstance(row.get("score_bool"), bool):
            raise RigInputError(f"{name}.score_bool is not complete")
        _finite_number(row.get("latency_seconds"), name=f"{name}.latency_seconds", minimum=0)
        _positive_int(row.get("reader_tokens"), name=f"{name}.reader_tokens")
        eligible = row.get("evidence_exposure_eligible")
        if not isinstance(eligible, bool):
            raise RigInputError(f"{name}.evidence_exposure_eligible must be boolean")
        exposed = row.get("evidence_exposed")
        if eligible and not isinstance(exposed, bool):
            raise RigInputError(f"{name}.evidence_exposed must be boolean when eligible")
        if not eligible and exposed is not None:
            raise RigInputError(f"{name}.evidence_exposed must be null when ineligible")
        if row.get("context_status") not in {"complete", "empty"}:
            raise RigInputError(f"{name}.context_status is not successful")
        if row.get("stack_fingerprint") != stack_digest:
            raise RigInputError(f"{name} has a different stack identity")
        _validate_activity(row.get("activity"), mode=mode, name=name)
        validated_rows.append(dict(row))
    if {domain for domain, _question_id in seen} != DOMAINS:
        raise RigInputError(f"{side} rows do not cover both Small domains")
    for domain in DOMAINS:
        domain_question_ids = sorted(
            question_id for row_domain, question_id in seen if row_domain == domain
        )
        if len(domain_question_ids) != official_question_counts[domain]:
            raise RigInputError(f"{side} {domain} official question count differs from its rows")
        if canonical_sha256(domain_question_ids) != official_question_ids_sha256[domain]:
            raise RigInputError(f"{side} {domain} official question digest differs from its rows")
        if not any(
            row["domain"] == domain and row["evidence_exposure_eligible"] for row in validated_rows
        ):
            raise RigInputError(f"{side} has no evidence-exposure-eligible {domain} rows")
    observed_lever_activity: dict[str, int] = {}
    for row in validated_rows:
        for lever, count in row["activity"]["lever_activity"].items():
            observed_lever_activity[lever] = observed_lever_activity.get(lever, 0) + count
    if lever_activity != observed_lever_activity:
        raise RigInputError(f"{side}.lever_activity does not match its row activity")
    question_order_sha256 = canonical_sha256(
        [[row["domain"], row["question_id"]] for row in validated_rows]
    )
    if raw.get("question_order_sha256") != question_order_sha256:
        raise RigInputError(f"{side} question-order digest does not match its rows")
    return {
        **raw,
        "experiment_phase": experiment_phase,
        "name": arm_name,
        "substrate": substrate,
        "preregistration_sha256": preregistration_sha256,
        "execution": execution,
        "configuration": dict(configuration),
        "geometry": geometry,
        "provider_usage": provider_usage,
        "lever_activity": dict(lever_activity),
        "official_question_count_by_domain": dict(official_question_counts),
        "official_question_ids_sha256_by_domain": dict(official_question_ids_sha256),
        "rows": validated_rows,
    }


def validate_pass(raw: object) -> dict[str, Any]:  # noqa: PLR0912
    if not isinstance(raw, dict) or raw.get("schema_version") != RUN_PAIR_SCHEMA_VERSION:
        raise RigInputError("paired pass schema is invalid")
    _require_exact_keys(raw, PAIRED_PASS_KEYS, name="paired pass")
    paired_digest = raw.get("paired_pass_sha256")
    unsigned = {key: value for key, value in raw.items() if key != "paired_pass_sha256"}
    if paired_digest != canonical_sha256(unsigned):
        raise RigInputError("paired-pass digest does not bind its content")
    experiment_id = _nonempty_string(raw.get("experiment_id"), name="experiment_id")
    experiment_phase = _nonempty_string(
        raw.get("experiment_phase"),
        name="experiment_phase",
    )
    if experiment_phase not in EXPERIMENT_PHASES - {"anchor"}:
        raise RigInputError("paired-pass experiment phase is invalid")
    pass_id = _nonempty_string(raw.get("pass_id"), name="pass_id")
    seed = _nonnegative_int(raw.get("seed"), name="seed")
    if experiment_phase == "aa":
        if raw.get("preregistration_sha256") != "":
            raise RigInputError("A/A paired pass cannot bind preregistration")
        preregistration_sha256 = ""
    else:
        preregistration_sha256 = _sha256_digest(
            raw.get("preregistration_sha256"),
            name="preregistration_sha256",
        )
    stack = validate_stack(raw.get("stack"))
    digest = stack_fingerprint(stack)
    arms = raw.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"left", "right"}:
        raise RigInputError("paired pass must contain left and right arms")
    left = validate_arm(arms["left"], stack_digest=digest, side="left")
    right = validate_arm(arms["right"], stack_digest=digest, side="right")
    for side, arm in (("left", left), ("right", right)):
        if arm["experiment_id"] != experiment_id:
            raise RigInputError(f"{side} arm experiment differs from the paired pass")
        if arm["experiment_phase"] != experiment_phase:
            raise RigInputError(f"{side} arm phase differs from the paired pass")
        if arm["pass_id"] != pass_id:
            raise RigInputError(f"{side} arm pass ID differs from the paired pass")
        if arm["seed"] != seed:
            raise RigInputError(f"{side} arm seed differs from the paired pass")
        if arm["preregistration_sha256"] != preregistration_sha256:
            raise RigInputError(f"{side} arm preregistration differs from the paired pass")
    for field in ("kind", "repository", "ref", "sha"):
        if left["execution"][field] != right["execution"][field]:
            raise RigInputError(f"paired arm executions differ for {field}")
    if left["execution"]["kind"] == "github" and (
        left["execution"]["workflow_ref"] != right["execution"]["workflow_ref"]
    ):
        raise RigInputError("paired arm executions differ for workflow_ref")
    if left["execution"]["run_id"] == right["execution"]["run_id"]:
        raise RigInputError("paired arms reused one execution run ID")
    left_ids = [(row["domain"], row["question_id"]) for row in left["rows"]]
    right_ids = [(row["domain"], row["question_id"]) for row in right["rows"]]
    if left_ids != right_ids:
        raise RigInputError("paired arms do not contain the same question order")
    return {
        **raw,
        "experiment_id": experiment_id,
        "experiment_phase": experiment_phase,
        "pass_id": pass_id,
        "seed": seed,
        "stack": stack,
        "arms": {"left": left, "right": right},
    }


def arm_summary(arm: dict[str, Any]) -> dict[str, Any]:
    rows = arm["rows"]
    scores = [float(row["score_bool"]) for row in rows]
    latencies = [float(row["latency_seconds"]) for row in rows]
    tokens = [float(row["reader_tokens"]) for row in rows]
    eligible_rows = [row for row in rows if row["evidence_exposure_eligible"]]
    exposure = [float(row["evidence_exposed"]) for row in eligible_rows]
    accuracy_by_domain = {
        domain: _mean([float(row["score_bool"]) for row in rows if row["domain"] == domain])
        for domain in sorted(DOMAINS)
    }
    exposure_by_domain = {
        domain: _mean(
            [float(row["evidence_exposed"]) for row in eligible_rows if row["domain"] == domain]
        )
        for domain in sorted(DOMAINS)
    }
    eligible_count_by_domain = {
        domain: sum(row["domain"] == domain for row in eligible_rows) for domain in sorted(DOMAINS)
    }
    return {
        "name": arm["name"],
        "question_count": len(rows),
        "accuracy": _mean(scores),
        "accuracy_by_domain": accuracy_by_domain,
        "evidence_exposure": _mean(exposure),
        "evidence_exposure_by_domain": exposure_by_domain,
        "evidence_exposure_eligible_count": len(eligible_rows),
        "evidence_exposure_eligible_count_by_domain": eligible_count_by_domain,
        "latency_mean_seconds": _mean(latencies),
        "latency_p95_seconds": _percentile(latencies, 0.95),
        "reader_tokens_mean": _mean(tokens),
        "reader_tokens_total": int(sum(tokens)),
        "provider_requests": int(arm["provider_usage"]["requests"]),
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
    if any(item["experiment_id"] != passes[0]["experiment_id"] for item in passes[1:]):
        raise RigInputError("paired passes changed experiment identity")
    if any(item["experiment_phase"] != passes[0]["experiment_phase"] for item in passes[1:]):
        raise RigInputError("paired passes changed experiment phase")
    if any(
        item["preregistration_sha256"] != passes[0]["preregistration_sha256"] for item in passes[1:]
    ):
        raise RigInputError("paired passes changed preregistration lineage")


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


def _aa_blocked_reason(status: str) -> str | None:
    return BLOCKED_REASON_SPAN_UNSTABLE if status == "RIG_BLOCKED" else None


def build_aa_receipt(raw_passes: list[dict[str, Any]]) -> dict[str, Any]:
    passes = [validate_pass(item) for item in raw_passes]
    if any(item["experiment_phase"] != "aa" for item in passes):
        raise RigInputError("A/A requires aa-phase paired passes")
    if len(passes) not in {INITIAL_AA_PASS_COUNT, EXTENDED_AA_PASS_COUNT}:
        raise RigInputError("A/A requires exactly three or five paired passes")
    _validate_pass_set(passes, expected_count=len(passes))
    first_arm = passes[0]["arms"]["left"]
    arm_contract = {
        "substrate": first_arm["substrate"],
        "configuration": first_arm["configuration"],
        "geometry": first_arm["geometry"],
    }
    pass_rows: list[dict[str, Any]] = []
    for item in passes:
        left = item["arms"]["left"]
        right = item["arms"]["right"]
        if left["substrate"] != "machine" or right["substrate"] != "machine":
            raise RigInputError("A/A requires two machine-substrate arms")
        if left["configuration"] != right["configuration"]:
            raise RigInputError("A/A arm configurations differ")
        if left["geometry"] != right["geometry"]:
            raise RigInputError("A/A arm geometry differs")
        current_contract = {
            "substrate": left["substrate"],
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
                "paired_pass_sha256": item["paired_pass_sha256"],
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
        "blocked_reason": _aa_blocked_reason(status),
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
    seeds: set[int] = set()
    absolute_deltas: list[float] = []
    for index, row in enumerate(pass_rows):
        if not isinstance(row, dict):
            raise RigInputError(f"A/A passes[{index}] is not an object")
        _require_exact_keys(row, AA_PASS_ROW_KEYS, name=f"A/A passes[{index}]")
        pass_id = _nonempty_string(row.get("pass_id"), name=f"A/A passes[{index}].pass_id")
        seed = _nonnegative_int(row.get("seed"), name=f"A/A passes[{index}].seed")
        _sha256_digest(
            row.get("paired_pass_sha256"),
            name=f"A/A passes[{index}].paired_pass_sha256",
        )
        if pass_id in pass_ids or seed in seeds:
            raise RigInputError("A/A receipt pass IDs and seeds must be unique")
        pass_ids.add(pass_id)
        seeds.add(seed)
        delta = _finite_number(
            row.get("accuracy_delta_pp"),
            name=f"A/A passes[{index}].accuracy_delta_pp",
        )
        left = _validate_arm_summary(row.get("left"), name=f"A/A passes[{index}].left")
        right = _validate_arm_summary(
            row.get("right"),
            name=f"A/A passes[{index}].right",
        )
        expected_delta = 100 * (float(right["accuracy"]) - float(left["accuracy"]))
        if not math.isclose(delta, expected_delta, abs_tol=1e-9):
            raise RigInputError("A/A accuracy delta does not match its arm summaries")
        expected_latency_delta = float(right["latency_mean_seconds"]) - float(
            left["latency_mean_seconds"]
        )
        actual_latency_delta = _finite_number(
            row.get("latency_delta_seconds"),
            name=f"A/A passes[{index}].latency_delta_seconds",
        )
        if not math.isclose(actual_latency_delta, expected_latency_delta, abs_tol=1e-9):
            raise RigInputError("A/A latency delta does not match its arm summaries")
        expected_token_delta = float(right["reader_tokens_mean"]) - float(
            left["reader_tokens_mean"]
        )
        actual_token_delta = _finite_number(
            row.get("reader_token_delta"),
            name=f"A/A passes[{index}].reader_token_delta",
        )
        if not math.isclose(actual_token_delta, expected_token_delta, abs_tol=1e-9):
            raise RigInputError("A/A token delta does not match its arm summaries")
        absolute_deltas.append(abs(delta))
    return absolute_deltas


def _validate_aa_metrics(raw: dict[str, Any], absolute_deltas: list[float]) -> None:
    observed_span = max(absolute_deltas)
    first_three_span = max(absolute_deltas[:INITIAL_AA_PASS_COUNT])
    expected_status, expected_stable = _aa_outcome(absolute_deltas)
    noise_floor = max(INITIAL_NOISE_FLOOR_PP, observed_span)
    if raw.get("status") != expected_status or raw.get("stabilized") is not expected_stable:
        raise RigInputError("A/A receipt status does not match its pass deltas")
    if raw.get("blocked_reason") != _aa_blocked_reason(expected_status):
        raise RigInputError("A/A blocked reason does not match its status")
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
    _require_exact_keys(
        arm_contract,
        frozenset({"substrate", "configuration", "geometry"}),
        name="A/A arm contract",
    )
    if arm_contract.get("substrate") != "machine":
        raise RigInputError("A/A arm substrate must be machine")
    configuration = arm_contract.get("configuration")
    geometry = arm_contract.get("geometry")
    if not isinstance(configuration, dict) or not configuration:
        raise RigInputError("A/A arm configuration is missing")
    _nonempty_string(configuration.get("retrieval_mode"), name="A/A retrieval mode")
    if not isinstance(geometry, dict):
        raise RigInputError("A/A arm geometry is missing")
    _validate_geometry(geometry, name="A/A geometry")


def validate_aa_receipt(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != AA_SCHEMA_VERSION:
        raise RigInputError("A/A receipt is missing or invalid")
    _require_exact_keys(raw, AA_RECEIPT_KEYS, name="A/A receipt")
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


def _aa_seeds(receipt: dict[str, Any]) -> set[int]:
    return {int(row["seed"]) for row in receipt["passes"]}


def build_anchor_receipt(
    raw_arm_run: dict[str, Any],
    *,
    aa_receipt: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_arm_run, dict):
        raise RigInputError("anchor arm run is invalid")
    arm_stack = validate_stack(raw_arm_run.get("stack"))
    arm = validate_arm(
        raw_arm_run,
        stack_digest=stack_fingerprint(arm_stack),
        side="anchor",
    )
    if arm["experiment_phase"] != "anchor":
        raise RigInputError("anchor requires an anchor-phase arm run")
    validated_aa = _require_passing_aa_receipt(aa_receipt)
    if arm_stack != validated_aa["stack"]:
        raise RigInputError("anchor stack does not match its A/A receipt")
    arm_contract = {
        "substrate": arm["substrate"],
        "configuration": arm["configuration"],
        "geometry": arm["geometry"],
    }
    if arm_contract != validated_aa["arm_contract"]:
        raise RigInputError("anchor arm configuration or geometry differs from A/A")
    if arm["seed"] in _aa_seeds(validated_aa):
        raise RigInputError("anchor seed reuses an A/A calibration seed")
    summary = arm_summary(arm)
    noise_floor = float(validated_aa["noise_floor_pp"])
    payload = {
        "schema_version": ANCHOR_SCHEMA_VERSION,
        "status": "PASS",
        "anchor_publishable": True,
        "comparative_claim_allowed": False,
        "historical_denominator_allowed": False,
        "stack": arm_stack,
        "aa_receipt_sha256": validated_aa["aa_receipt_sha256"],
        "aa_pass_count": validated_aa["pass_count"],
        "aa_observed_span_pp": validated_aa["observed_span_pp"],
        "arm_run_sha256": arm["arm_run_sha256"],
        "arm": arm_contract,
        "metrics": {**summary, "noise_floor_pp": noise_floor},
    }
    payload["anchor_receipt_sha256"] = canonical_sha256(payload)
    _require_exact_keys(payload, ANCHOR_RECEIPT_KEYS, name="anchor receipt")
    return payload


def freeze_preregistration(  # noqa: PLR0912, PLR0915
    raw: dict[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
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
    required = (
        {"machine_configuration", "naive_configuration", "shipping_geometry", "matched_geometry"}
        if kind == "race"
        else {
            "race_receipt",
            "control_configuration",
            "treatment_configuration",
            "control_geometry",
            "treatment_geometry",
            "included_levers",
            "replay_survivors",
        }
    )
    missing = sorted(key for key in required if key not in raw)
    if missing:
        raise RigInputError(f"{kind} preregistration is missing {missing}")
    reject_scores(
        {key: value for key, value in raw.items() if key not in {"aa_receipt", "race_receipt"}}
    )
    allowed = required | {"created_at", "stack", "seeds", "aa_receipt", "decision_rule"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise RigInputError(f"{kind} preregistration has unknown fields {unknown}")
    aa_receipt = _require_passing_aa_receipt(raw.get("aa_receipt"))
    stack = validate_stack(raw.get("stack"))
    if stack != aa_receipt["stack"]:
        raise RigInputError("preregistration stack does not match its A/A receipt")
    if kind == "race":
        shipping_geometry = raw["shipping_geometry"]
        machine_geometry = (
            shipping_geometry.get("machine") if isinstance(shipping_geometry, dict) else None
        )
        expected_aa_contract = {
            "substrate": "machine",
            "configuration": raw["machine_configuration"],
            "geometry": machine_geometry,
        }
    else:
        expected_aa_contract = {
            "substrate": "machine",
            "configuration": raw["control_configuration"],
            "geometry": raw["control_geometry"],
        }
    if aa_receipt["arm_contract"] != expected_aa_contract:
        raise RigInputError(f"A/A arm does not match the preregistered {kind} control")
    seeds = raw.get("seeds")
    if not isinstance(seeds, list) or len(seeds) != PAIRED_PASS_COUNT:
        raise RigInputError("preregistration must freeze exactly three seeds")
    normalized_seeds = [_nonnegative_int(seed, name="seed") for seed in seeds]
    if len(set(normalized_seeds)) != PAIRED_PASS_COUNT:
        raise RigInputError("preregistered seeds must be unique")
    if set(normalized_seeds) & _aa_seeds(aa_receipt):
        raise RigInputError("preregistered seeds must not reuse A/A calibration seeds")
    expected_rule = RACE_DECISION_RULE if kind == "race" else RENDER_DECISION_RULE
    if raw.get("decision_rule") != expected_rule:
        raise RigInputError(f"{kind} decision rule does not match the v1.3 contract")
    race_receipt: dict[str, Any] | None = None
    if kind == "render":
        race_receipt = validate_race_receipt(raw.get("race_receipt"))
        if race_receipt["stack"] != stack:
            raise RigInputError("render race stack does not match preregistration")
        if race_receipt["aa_receipt_sha256"] != aa_receipt["aa_receipt_sha256"]:
            raise RigInputError("render race does not bind the machine A/A receipt")
        control_geometry = _validate_geometry(
            raw["control_geometry"],
            name="render control geometry",
        )
        treatment_geometry = _validate_geometry(
            raw["treatment_geometry"],
            name="render treatment geometry",
        )
        comparable_control = {
            key: value
            for key, value in control_geometry.items()
            if key != "max_context_total_chars"
        }
        comparable_treatment = {
            key: value
            for key, value in treatment_geometry.items()
            if key != "max_context_total_chars"
        }
        if comparable_control != comparable_treatment:
            raise RigInputError("render geometry may differ only for max_context_total_chars")
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
    if race_receipt is not None:
        payload["race_receipt"] = race_receipt
        payload["race_receipt_sha256"] = race_receipt["race_receipt_sha256"]
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
                "race_receipt_sha256",
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


def _race_decision(
    deltas: list[float],
    latency_ratios: list[float],
    *,
    noise_floor: float,
) -> str:
    mean_delta = _mean(deltas)
    if mean_delta >= noise_floor and all(delta > 0 for delta in deltas):
        return "RETAIN_MACHINE_ON_ACCURACY"
    if (
        mean_delta < noise_floor
        and all(delta < noise_floor for delta in deltas)
        and all(ratio <= NAIVE_MAXIMUM_LATENCY_RATIO for ratio in latency_ratios)
    ):
        return "V1_4_MACHINE_DELETION_CANDIDATE"
    return "INCONCLUSIVE_KEEP_MACHINE_DEFAULT"


def build_race_receipt(  # noqa: PLR0912
    raw_preregistration: dict[str, Any],
    raw_passes: list[dict[str, Any]],
    raw_matched_control: dict[str, Any],
) -> dict[str, Any]:
    preregistration = validate_preregistration(raw_preregistration, kind="race")
    passes = [validate_pass(item) for item in raw_passes]
    if any(item["experiment_phase"] != "race" for item in passes):
        raise RigInputError("race requires race-phase paired passes")
    _require_preregistered_passes(preregistration, passes)
    noise_floor = float(preregistration["noise_floor_pp"])
    pass_rows: list[dict[str, Any]] = []
    for item in passes:
        machine = _arm_by_name(item, "machine")
        naive = _arm_by_name(item, "naive")
        if machine["substrate"] != "machine" or naive["substrate"] != "naive":
            raise RigInputError("race arm roles do not match their substrates")
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
        if machine_summary["latency_mean_seconds"] <= 0:
            raise RigInputError("machine latency must be positive for the race ratio")
        pass_rows.append(
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "paired_pass_sha256": item["paired_pass_sha256"],
                "machine": machine_summary,
                "naive": naive_summary,
                "accuracy_delta_pp": 100
                * (machine_summary["accuracy"] - naive_summary["accuracy"]),
                "naive_to_machine_latency_ratio": (
                    naive_summary["latency_mean_seconds"] / machine_summary["latency_mean_seconds"]
                ),
            }
        )
    matched = validate_pass(raw_matched_control)
    if matched["experiment_phase"] != "race":
        raise RigInputError("matched control requires a race-phase paired pass")
    if matched["stack"] != preregistration["stack"]:
        raise RigInputError("matched-character control changed the stack")
    if matched.get("preregistration_sha256") != preregistration["preregistration_sha256"]:
        raise RigInputError("matched-character control is not preregistration-bound")
    matched_machine = _arm_by_name(matched, "machine")
    matched_naive = _arm_by_name(matched, "naive")
    if matched["seed"] in set(preregistration["seeds"]) | _aa_seeds(preregistration["aa_receipt"]):
        raise RigInputError("matched-character control must use a fresh seed")
    if (
        matched_machine["substrate"] != "machine"
        or matched_naive["substrate"] != "naive"
        or matched_machine["configuration"] != preregistration["machine_configuration"]
        or matched_naive["configuration"] != preregistration["naive_configuration"]
    ):
        raise RigInputError("matched-character control arm contract differs")
    if (
        matched_machine["geometry"] != matched_naive["geometry"]
        or matched_machine["geometry"] != preregistration["matched_geometry"]
    ):
        raise RigInputError("matched-character control geometry is not matched")
    deltas = [float(item["accuracy_delta_pp"]) for item in pass_rows]
    latency_ratios = [float(item["naive_to_machine_latency_ratio"]) for item in pass_rows]
    mean_delta = _mean(deltas)
    decision = _race_decision(deltas, latency_ratios, noise_floor=noise_floor)
    selected_render_substrate = (
        "naive" if decision == "V1_4_MACHINE_DELETION_CANDIDATE" else "machine"
    )
    payload = {
        "schema_version": RACE_SCHEMA_VERSION,
        "status": "PASS",
        "decision": decision,
        "selected_render_substrate": selected_render_substrate,
        "machine_deleted": False,
        "naive_default": False,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "aa_receipt_sha256": preregistration["aa_receipt_sha256"],
        "stack": preregistration["stack"],
        "noise_floor_pp": noise_floor,
        "aa_span_pp": preregistration["aa_span_pp"],
        "mean_accuracy_delta_pp": mean_delta,
        "every_pass_positive": all(delta > 0 for delta in deltas),
        "passes": pass_rows,
        "matched_character_control": {
            "pass_id": matched["pass_id"],
            "seed": matched["seed"],
            "paired_pass_sha256": matched["paired_pass_sha256"],
            "machine": arm_summary(matched_machine),
            "naive": arm_summary(matched_naive),
        },
        "decision_rule": RACE_DECISION_RULE,
    }
    payload["race_receipt_sha256"] = canonical_sha256(payload)
    return payload


def _validate_arm_summary(raw: object, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is not an arm summary")
    _require_exact_keys(raw, ARM_SUMMARY_KEYS, name=name)
    _nonempty_string(raw.get("name"), name=f"{name}.name")
    question_count = _positive_int(raw.get("question_count"), name=f"{name}.question_count")
    accuracy = _finite_number(raw.get("accuracy"), name=f"{name}.accuracy", minimum=0)
    if accuracy > 1:
        raise RigInputError(f"{name}.accuracy exceeds one")
    for field in ("accuracy_by_domain", "evidence_exposure_by_domain"):
        values = raw.get(field)
        if not isinstance(values, dict) or set(values) != DOMAINS:
            raise RigInputError(f"{name}.{field} must cover both Small domains")
        for domain, value in values.items():
            rate = _finite_number(value, name=f"{name}.{field}.{domain}", minimum=0)
            if rate > 1:
                raise RigInputError(f"{name}.{field}.{domain} exceeds one")
    exposure = _finite_number(
        raw.get("evidence_exposure"),
        name=f"{name}.evidence_exposure",
        minimum=0,
    )
    if exposure > 1:
        raise RigInputError(f"{name}.evidence_exposure exceeds one")
    eligible = _positive_int(
        raw.get("evidence_exposure_eligible_count"),
        name=f"{name}.eligible",
    )
    eligible_by_domain = raw.get("evidence_exposure_eligible_count_by_domain")
    if not isinstance(eligible_by_domain, dict) or set(eligible_by_domain) != DOMAINS:
        raise RigInputError(f"{name}.eligible_by_domain must cover both Small domains")
    domain_eligible = [
        _positive_int(value, name=f"{name}.eligible_by_domain.{domain}")
        for domain, value in eligible_by_domain.items()
    ]
    if eligible > question_count or sum(domain_eligible) != eligible:
        raise RigInputError(f"{name}.eligible counts are inconsistent")
    _finite_number(
        raw.get("latency_mean_seconds"),
        name=f"{name}.latency_mean_seconds",
        minimum=0,
    )
    _finite_number(
        raw.get("latency_p95_seconds"),
        name=f"{name}.latency_p95_seconds",
        minimum=0,
    )
    _finite_number(
        raw.get("reader_tokens_mean"),
        name=f"{name}.reader_tokens_mean",
        minimum=0,
    )
    _positive_int(raw.get("reader_tokens_total"), name=f"{name}.reader_tokens_total")
    _positive_int(raw.get("provider_requests"), name=f"{name}.provider_requests")
    _positive_int(raw.get("activity_events"), name=f"{name}.activity_events")
    return dict(raw)


def validate_race_receipt(raw: object) -> dict[str, Any]:  # noqa: PLR0912, PLR0915
    if not isinstance(raw, dict) or raw.get("schema_version") != RACE_SCHEMA_VERSION:
        raise RigInputError("race receipt is missing or invalid")
    _require_exact_keys(raw, RACE_RECEIPT_KEYS, name="race receipt")
    unsigned = {key: value for key, value in raw.items() if key != "race_receipt_sha256"}
    if raw.get("race_receipt_sha256") != canonical_sha256(unsigned):
        raise RigInputError("race receipt digest does not bind its content")
    if raw.get("status") != "PASS" or raw.get("decision_rule") != RACE_DECISION_RULE:
        raise RigInputError("race receipt is not a completed v1.3 decision")
    stack = validate_stack(raw.get("stack"))
    _sha256_digest(raw.get("preregistration_sha256"), name="race preregistration digest")
    _sha256_digest(raw.get("aa_receipt_sha256"), name="race A/A digest")
    noise_floor = _finite_number(raw.get("noise_floor_pp"), name="race noise floor", minimum=0)
    _finite_number(raw.get("aa_span_pp"), name="race A/A span", minimum=0)
    pass_rows = raw.get("passes")
    if not isinstance(pass_rows, list) or len(pass_rows) != PAIRED_PASS_COUNT:
        raise RigInputError("race receipt must contain exactly three passes")
    pass_ids: set[str] = set()
    seeds: set[int] = set()
    paired_digests: set[str] = set()
    deltas: list[float] = []
    latency_ratios: list[float] = []
    for index, row in enumerate(pass_rows):
        name = f"race passes[{index}]"
        if not isinstance(row, dict):
            raise RigInputError(f"{name} is not an object")
        _require_exact_keys(row, RACE_PASS_ROW_KEYS, name=name)
        pass_id = _nonempty_string(row.get("pass_id"), name=f"{name}.pass_id")
        seed = _nonnegative_int(row.get("seed"), name=f"{name}.seed")
        paired_digest = _sha256_digest(
            row.get("paired_pass_sha256"),
            name=f"{name}.paired_pass_sha256",
        )
        if pass_id in pass_ids or seed in seeds or paired_digest in paired_digests:
            raise RigInputError("race receipt pass lineage must be unique")
        pass_ids.add(pass_id)
        seeds.add(seed)
        paired_digests.add(paired_digest)
        machine = _validate_arm_summary(row.get("machine"), name=f"{name}.machine")
        naive = _validate_arm_summary(row.get("naive"), name=f"{name}.naive")
        if machine["name"] != "machine" or naive["name"] != "naive":
            raise RigInputError("race arm roles are invalid")
        expected_delta = 100 * (float(machine["accuracy"]) - float(naive["accuracy"]))
        delta = _finite_number(row.get("accuracy_delta_pp"), name=f"{name}.delta")
        if not math.isclose(delta, expected_delta, abs_tol=1e-9):
            raise RigInputError("race accuracy delta does not match its arm summaries")
        machine_latency = float(machine["latency_mean_seconds"])
        if machine_latency <= 0:
            raise RigInputError("race machine latency must be positive")
        expected_ratio = float(naive["latency_mean_seconds"]) / machine_latency
        ratio = _finite_number(
            row.get("naive_to_machine_latency_ratio"),
            name=f"{name}.latency_ratio",
            minimum=0,
        )
        if not math.isclose(ratio, expected_ratio, abs_tol=1e-9):
            raise RigInputError("race latency ratio does not match its arm summaries")
        deltas.append(delta)
        latency_ratios.append(ratio)
    expected_decision = _race_decision(deltas, latency_ratios, noise_floor=noise_floor)
    if raw.get("decision") != expected_decision:
        raise RigInputError("race decision does not match its pass rows")
    expected_substrate = (
        "naive" if expected_decision == "V1_4_MACHINE_DELETION_CANDIDATE" else "machine"
    )
    if raw.get("selected_render_substrate") != expected_substrate:
        raise RigInputError("race selected render substrate does not match its decision")
    if raw.get("machine_deleted") is not False or raw.get("naive_default") is not False:
        raise RigInputError("race receipt cannot change the v1.3 shipping default")
    mean_delta = _mean(deltas)
    if not math.isclose(
        _finite_number(raw.get("mean_accuracy_delta_pp"), name="race mean delta"),
        mean_delta,
        abs_tol=1e-9,
    ):
        raise RigInputError("race mean delta does not match its pass rows")
    if raw.get("every_pass_positive") != all(delta > 0 for delta in deltas):
        raise RigInputError("race sign verdict does not match its pass rows")
    matched = raw.get("matched_character_control")
    if not isinstance(matched, dict):
        raise RigInputError("race matched-character control is missing")
    _require_exact_keys(matched, MATCHED_CONTROL_KEYS, name="matched-character control")
    _nonempty_string(matched.get("pass_id"), name="matched pass ID")
    _nonnegative_int(matched.get("seed"), name="matched seed")
    matched_digest = _sha256_digest(
        matched.get("paired_pass_sha256"),
        name="matched pass digest",
    )
    if (
        matched.get("pass_id") in pass_ids
        or matched.get("seed") in seeds
        or matched_digest in paired_digests
    ):
        raise RigInputError("race matched-character lineage must be fresh")
    matched_machine = _validate_arm_summary(matched.get("machine"), name="matched machine")
    matched_naive = _validate_arm_summary(matched.get("naive"), name="matched naive")
    if matched_machine["name"] != "machine" or matched_naive["name"] != "naive":
        raise RigInputError("race matched-character arm roles are invalid")
    return {**raw, "stack": stack}


def build_render_receipt(  # noqa: PLR0912, PLR0915
    raw_preregistration: dict[str, Any],
    raw_passes: list[dict[str, Any]],
) -> dict[str, Any]:
    preregistration = validate_preregistration(raw_preregistration, kind="render")
    included_levers = preregistration["included_levers"]
    replay_survivors = preregistration["replay_survivors"]
    if (
        not isinstance(included_levers, list)
        or not included_levers
        or len(set(included_levers)) != len(included_levers)
        or any(not isinstance(lever, str) or not lever for lever in included_levers)
        or not isinstance(replay_survivors, dict)
        or set(replay_survivors) != set(included_levers)
        or any(replay_survivors.get(lever) is not True for lever in included_levers)
    ):
        raise RigInputError("render bundle contains a lever that did not survive replay")
    race_receipt = validate_race_receipt(preregistration["race_receipt"])
    selected_substrate = race_receipt["selected_render_substrate"]
    if selected_substrate == "naive":
        if raw_passes:
            raise RigInputError("render passes are forbidden when the race selects naive")
        payload = {
            "schema_version": RENDER_SCHEMA_VERSION,
            "status": "NOT_APPLICABLE",
            "decision": "NOT_APPLICABLE",
            "reason": "race_selected_naive_substrate",
            "selected_render_substrate": "naive",
            "preregistration_sha256": preregistration["preregistration_sha256"],
            "race_receipt_sha256": race_receipt["race_receipt_sha256"],
            "stack": preregistration["stack"],
            "included_levers": included_levers,
            "noise_floor_pp": preregistration["noise_floor_pp"],
            "mean_accuracy_delta_pp": None,
            "mean_latency_regression_seconds": None,
            "mean_reader_token_regression_ratio": None,
            "exposure_gain_pp_by_domain": dict.fromkeys(sorted(DOMAINS)),
            "gates": {
                "exposure": None,
                "accuracy": None,
                "latency": None,
                "reader_tokens": None,
            },
            "passes": [],
            "decision_rule": RENDER_DECISION_RULE,
        }
        payload["render_receipt_sha256"] = canonical_sha256(payload)
        _require_exact_keys(payload, RENDER_RECEIPT_KEYS, name="render receipt")
        return payload
    passes = [validate_pass(item) for item in raw_passes]
    if any(item["experiment_phase"] != "render" for item in passes):
        raise RigInputError("render requires render-phase paired passes")
    _require_preregistered_passes(preregistration, passes)
    pass_rows: list[dict[str, Any]] = []
    for item in passes:
        control = _arm_by_name(item, "render_control")
        treatment = _arm_by_name(item, "render_treatment")
        if control["substrate"] != "machine" or treatment["substrate"] != "machine":
            raise RigInputError("render arms must use the machine substrate")
        if control["configuration"] != preregistration["control_configuration"]:
            raise RigInputError("render control differs from preregistration")
        if treatment["configuration"] != preregistration["treatment_configuration"]:
            raise RigInputError("render treatment differs from preregistration")
        if (
            control["geometry"] != preregistration["control_geometry"]
            or treatment["geometry"] != preregistration["treatment_geometry"]
        ):
            raise RigInputError("render bundle changed its preregistered geometry")
        lever_activity = treatment["lever_activity"]
        if set(lever_activity) != set(included_levers):
            raise RigInputError("render treatment has an included lever with no activity")
        control_summary = arm_summary(control)
        treatment_summary = arm_summary(treatment)
        pass_rows.append(
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "paired_pass_sha256": item["paired_pass_sha256"],
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
    payload = {
        "schema_version": RENDER_SCHEMA_VERSION,
        "status": "PASS",
        "decision": decision,
        "reason": None,
        "selected_render_substrate": "machine",
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "race_receipt_sha256": race_receipt["race_receipt_sha256"],
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
    payload["render_receipt_sha256"] = canonical_sha256(payload)
    _require_exact_keys(payload, RENDER_RECEIPT_KEYS, name="render receipt")
    return payload


DISPATCH_LEDGER_KEYS = frozenset(
    {"schema_version", "repository", "fetched_at", "source", "attempts"}
)
DISPATCH_LEDGER_SOURCE_KEYS = frozenset({"tool", "run_endpoint", "jobs_endpoint", "builder_query"})
DISPATCH_ATTEMPT_KEYS = frozenset({"controller", "builders"})
LEDGER_RUN_KEYS = frozenset(
    {
        "id",
        "name",
        "path",
        "display_title",
        "head_sha",
        "head_branch",
        "event",
        "status",
        "conclusion",
        "run_attempt",
        "created_at",
        "updated_at",
        "run_started_at",
        "html_url",
        "repository",
    }
)
LEDGER_BUILDER_KEYS = LEDGER_RUN_KEYS | {"jobs"}
LEDGER_JOB_KEYS = frozenset(
    {
        "id",
        "run_id",
        "run_attempt",
        "name",
        "status",
        "conclusion",
        "started_at",
        "completed_at",
        "head_sha",
        "html_url",
    }
)
RIG_BLOCKED_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "blocked_reason",
        "paid_benchmark_allowed",
        "score_claim_allowed",
        "repository",
        "head_branch",
        "head_shas",
        "controller_workflow",
        "builder_workflow",
        "builder_arm_id",
        "required_attempt_count",
        "attempt_count",
        "completed_pass_count",
        "first_dispatched_at",
        "last_dispatched_at",
        "attempts",
        "ledger_fetched_at",
        "ledger_sha256",
        "ledger_provenance",
        "github_verification",
        "exhaustion_rule",
        "rig_blocked_receipt_sha256",
    }
)
GITHUB_VERIFICATION_KEYS = frozenset({"verified_at", "run_count", "job_count", "builder_query"})
GitHubFetch = Callable[[str], list[Any]]
RIG_BLOCKED_ATTEMPT_KEYS = frozenset(
    {"experiment_id", "head_sha", "head_branch", "controller", "builders"}
)
RIG_BLOCKED_RUN_KEYS = frozenset(
    {"run_id", "run_attempt", "event", "conclusion", "created_at", "updated_at", "html_url"}
)
RIG_BLOCKED_BUILDER_KEYS = RIG_BLOCKED_RUN_KEYS | {"head_sha", "official_jobs"}
RIG_BLOCKED_JOB_KEYS = frozenset({"job_id", "conclusion", "started_at", "completed_at", "html_url"})


def _parse_timestamp(value: object, *, name: str) -> datetime:
    text = _nonempty_string(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RigInputError(f"{name} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RigInputError(f"{name} must carry a UTC offset")
    return parsed


def _optional_timestamp(value: object, *, name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value, name=name)


def _git_sha(value: object, *, name: str) -> str:
    sha = _nonempty_string(value, name=name)
    if len(sha) != GIT_SHA_LENGTH or any(character not in "0123456789abcdef" for character in sha):
        raise RigInputError(f"{name} must be a full lowercase Git SHA")
    return sha


def _ledger_run(raw: object, *, name: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is not an object")
    _require_exact_keys(raw, keys, name=name)
    _positive_int(raw.get("id"), name=f"{name}.id")
    _positive_int(raw.get("run_attempt"), name=f"{name}.run_attempt")
    if raw.get("status") != "completed":
        raise RigInputError(f"{name} has not completed")
    if raw.get("conclusion") not in COMPLETED_RUN_CONCLUSIONS:
        raise RigInputError(f"{name} conclusion is not a completed verdict")
    if raw.get("event") != "workflow_dispatch":
        raise RigInputError(f"{name} was not a workflow dispatch")
    created = _parse_timestamp(raw.get("created_at"), name=f"{name}.created_at")
    updated = _parse_timestamp(raw.get("updated_at"), name=f"{name}.updated_at")
    _parse_timestamp(raw.get("run_started_at"), name=f"{name}.run_started_at")
    if updated < created:
        raise RigInputError(f"{name} finished before it was created")
    for field in ("name", "path", "display_title", "head_branch", "html_url", "repository"):
        _nonempty_string(raw.get(field), name=f"{name}.{field}")
    _git_sha(raw.get("head_sha"), name=f"{name}.head_sha")
    return dict(raw)


def _require_job_interval(raw: dict[str, Any], *, name: str) -> None:
    started = _optional_timestamp(raw.get("started_at"), name=f"{name}.started_at")
    completed = _optional_timestamp(raw.get("completed_at"), name=f"{name}.completed_at")
    if started is None or completed is None or completed >= started:
        return
    reversal = (started - completed).total_seconds()
    if raw.get("conclusion") != "skipped" or reversal > SKIPPED_JOB_MAX_CLOCK_SKEW_SECONDS:
        raise RigInputError(f"{name} completed before it started")


def _ledger_job(raw: object, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is not an object")
    _require_exact_keys(raw, LEDGER_JOB_KEYS, name=name)
    _positive_int(raw.get("id"), name=f"{name}.id")
    _positive_int(raw.get("run_id"), name=f"{name}.run_id")
    _positive_int(raw.get("run_attempt"), name=f"{name}.run_attempt")
    if raw.get("status") != "completed":
        raise RigInputError(f"{name} has not completed")
    if raw.get("conclusion") not in COMPLETED_JOB_CONCLUSIONS:
        raise RigInputError(f"{name} conclusion is not a completed verdict")
    _require_job_interval(raw, name=name)
    _nonempty_string(raw.get("name"), name=f"{name}.name")
    _nonempty_string(raw.get("html_url"), name=f"{name}.html_url")
    _git_sha(raw.get("head_sha"), name=f"{name}.head_sha")
    return dict(raw)


def _official_jobs(
    raw_jobs: object,
    *,
    builder: dict[str, Any],
    name: str,
) -> dict[str, dict[str, Any] | None]:
    if not isinstance(raw_jobs, list):
        raise RigInputError(f"{name}.jobs is not a list")
    by_name: dict[str, dict[str, Any]] = {}
    official_names = set(OFFICIAL_JOB_NAMES.values())
    for index, raw_job in enumerate(raw_jobs):
        job = _ledger_job(raw_job, name=f"{name}.jobs[{index}]")
        if (
            job["run_id"] != builder["id"]
            or job["run_attempt"] != builder["run_attempt"]
            or job["head_sha"] != builder["head_sha"]
        ):
            raise RigInputError(f"{name}.jobs[{index}] does not belong to its builder run")
        if job["name"] in official_names:
            if job["name"] in by_name:
                raise RigInputError(f"{name} reports the official job {job['name']!r} twice")
            by_name[job["name"]] = job
    result: dict[str, dict[str, Any] | None] = {}
    for key, job_name in OFFICIAL_JOB_NAMES.items():
        job = by_name.get(job_name)
        if job is None:
            if key == "combined_receipt":
                result[key] = None
                continue
            raise RigInputError(f"{name} never ran the official {key} Small domain")
        result[key] = {
            "job_id": job["id"],
            "conclusion": job["conclusion"],
            "started_at": job["started_at"],
            "completed_at": job["completed_at"],
            "html_url": job["html_url"],
        }
    return result


def _job_succeeded(job: dict[str, Any] | None) -> bool:
    return job is not None and job["conclusion"] == "success"


def _completed_two_domain_arm(official_jobs: dict[str, dict[str, Any] | None]) -> bool:
    domains_succeeded = all(_job_succeeded(official_jobs[domain]) for domain in sorted(DOMAINS))
    return domains_succeeded or _job_succeeded(official_jobs["combined_receipt"])


def _run_projection(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "event": run["event"],
        "conclusion": run["conclusion"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "html_url": run["html_url"],
    }


def _dispatch_attempt(  # noqa: PLR0912
    raw: object,
    *,
    index: int,
    repository: str,
) -> dict[str, Any]:
    name = f"attempts[{index}]"
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is not an object")
    _require_exact_keys(raw, DISPATCH_ATTEMPT_KEYS, name=name)
    controller = _ledger_run(raw.get("controller"), name=f"{name}.controller", keys=LEDGER_RUN_KEYS)
    if controller["repository"] != repository:
        raise RigInputError(f"{name}.controller belongs to another repository")
    if controller["path"] != CONTROLLER_WORKFLOW_PATH:
        raise RigInputError(f"{name}.controller is not the release A/A controller workflow")
    if controller["conclusion"] != "success":
        raise RigInputError(f"{name}.controller did not complete its dispatch")
    if controller["head_branch"] != TRUSTED_BRANCH:
        raise RigInputError(f"{name}.controller is not on the trusted release branch")
    prefix = f"{CONTROLLER_WORKFLOW_NAME} "
    title = controller["display_title"]
    if not title.startswith(prefix):
        raise RigInputError(f"{name}.controller title does not carry an experiment id")
    experiment_id = title[len(prefix) :]
    if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
        raise RigInputError(f"{name}.controller experiment id is not path-safe")
    head_sha = controller["head_sha"]
    head_branch = controller["head_branch"]
    controller_created = _parse_timestamp(controller["created_at"], name="controller.created_at")
    raw_builders = raw.get("builders")
    if not isinstance(raw_builders, list) or not raw_builders:
        raise RigInputError(f"{name}.controller dispatched no builder run")
    expected_title = f"{BUILDER_WORKFLOW_NAME} aa-{controller['id']} {DISPATCH_BUILDER_ARM_ID}"
    builders: list[dict[str, Any]] = []
    for builder_index, raw_builder in enumerate(raw_builders):
        builder_name = f"{name}.builders[{builder_index}]"
        builder = _ledger_run(raw_builder, name=builder_name, keys=LEDGER_BUILDER_KEYS)
        if builder["repository"] != repository:
            raise RigInputError(f"{builder_name} belongs to another repository")
        if builder["path"] != BUILDER_WORKFLOW_PATH:
            raise RigInputError(f"{builder_name} is not the sealed builder workflow")
        if builder["display_title"] != expected_title:
            raise RigInputError(
                f"{builder_name} was not dispatched by controller {controller['id']}"
            )
        if builder["head_sha"] != head_sha:
            raise RigInputError(f"{builder_name} head SHA differs from its controller dispatch")
        if builder["head_branch"] != head_branch:
            raise RigInputError(f"{builder_name} branch differs from its controller dispatch")
        builder_created = _parse_timestamp(builder["created_at"], name="builder.created_at")
        if builder_created < controller_created:
            raise RigInputError(f"{builder_name} predates its controller dispatch")
        official_jobs = _official_jobs(builder["jobs"], builder=builder, name=builder_name)
        if _completed_two_domain_arm(official_jobs):
            raise RigInputError(
                f"{experiment_id} completed a two-domain arm run in builder {builder['id']}; "
                "that is A/A data, not dispatch exhaustion"
            )
        builders.append(
            {
                **_run_projection(builder),
                "head_sha": builder["head_sha"],
                "official_jobs": official_jobs,
            }
        )
    return {
        "experiment_id": experiment_id,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "controller": _run_projection(controller),
        "builders": builders,
    }


def validate_dispatch_ledger(ledger: object) -> list[dict[str, Any]]:
    """Validate one observed dispatch ledger and return its bound attempt rows."""
    if (
        not isinstance(ledger, dict)
        or ledger.get("schema_version") != DISPATCH_LEDGER_SCHEMA_VERSION
    ):
        raise RigInputError("dispatch ledger schema is missing or invalid")
    _require_exact_keys(ledger, DISPATCH_LEDGER_KEYS, name="dispatch ledger")
    repository = _nonempty_string(ledger.get("repository"), name="dispatch ledger repository")
    if repository != TRUSTED_REPOSITORY:
        raise RigInputError("dispatch ledger repository is not the trusted release repository")
    _parse_timestamp(ledger.get("fetched_at"), name="dispatch ledger fetched_at")
    source = ledger.get("source")
    if not isinstance(source, dict):
        raise RigInputError("dispatch ledger source is missing")
    _require_exact_keys(source, DISPATCH_LEDGER_SOURCE_KEYS, name="dispatch ledger source")
    for field in DISPATCH_LEDGER_SOURCE_KEYS:
        _nonempty_string(source.get(field), name=f"dispatch ledger source.{field}")
    raw_attempts = ledger.get("attempts")
    if not isinstance(raw_attempts, list):
        raise RigInputError("dispatch ledger attempts is not a list")
    attempts = [
        _dispatch_attempt(item, index=index, repository=repository)
        for index, item in enumerate(raw_attempts)
    ]
    if len(attempts) < EXTENDED_AA_PASS_COUNT:
        raise RigInputError(
            f"dispatch exhaustion requires at least {EXTENDED_AA_PASS_COUNT} controller "
            f"dispatches; the ledger holds {len(attempts)}"
        )
    return attempts


def gh_api_fetch(endpoint: str) -> list[Any]:
    """Fetch every page of one GitHub REST endpoint through the authenticated gh CLI."""
    completed = subprocess.run(  # noqa: S603
        ["gh", "api", "--paginate", "--slurp", endpoint],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RigInputError(f"gh api {endpoint} failed: {completed.stderr.strip()}")
    try:
        pages = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RigInputError(f"gh api {endpoint} returned invalid JSON") from exc
    if not isinstance(pages, list):
        raise RigInputError(f"gh api {endpoint} did not return slurped pages")
    return pages


def _github_object(fetch: GitHubFetch, endpoint: str) -> dict[str, Any]:
    pages = fetch(endpoint)
    if len(pages) != 1 or not isinstance(pages[0], dict):
        raise RigInputError(f"GitHub {endpoint} did not return one object")
    return pages[0]


def _github_collection(fetch: GitHubFetch, endpoint: str, *, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in fetch(endpoint):
        if not isinstance(page, dict) or not isinstance(page.get(key), list):
            raise RigInputError(f"GitHub {endpoint} did not return {key} pages")
        items.extend(item for item in page[key] if isinstance(item, dict))
    return items


def _project_github_run(raw: dict[str, Any]) -> dict[str, Any]:
    projected = {key: raw.get(key) for key in LEDGER_RUN_KEYS if key != "repository"}
    repository = raw.get("repository")
    projected["repository"] = repository.get("full_name") if isinstance(repository, dict) else None
    return projected


def _project_github_job(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: raw.get(key) for key in LEDGER_JOB_KEYS}


def _differing_fields(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    return sorted(
        key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key)
    )


def _require_github_equal(expected: dict[str, Any], actual: dict[str, Any], *, name: str) -> None:
    differing = _differing_fields(expected, actual)
    if differing:
        raise RigInputError(f"{name} differs from GitHub on {', '.join(differing)}")


def verify_dispatch_ledger(ledger: dict[str, Any], *, fetch: GitHubFetch) -> dict[str, Any]:
    """Re-fetch every run and job in the ledger and require field-for-field equality."""
    validate_dispatch_ledger(ledger)
    repository = ledger["repository"]
    workflow = BUILDER_WORKFLOW_PATH.rsplit("/", 1)[-1]
    run_count = 0
    job_count = 0
    for index, attempt in enumerate(ledger["attempts"]):
        name = f"attempts[{index}]"
        controller = attempt["controller"]
        live_controller = _project_github_run(
            _github_object(
                fetch,
                GITHUB_RUN_ENDPOINT.format(repository=repository, run_id=controller["id"]),
            )
        )
        _require_github_equal(controller, live_controller, name=f"{name}.controller")
        run_count += 1
        listing = _github_collection(
            fetch,
            GITHUB_BUILDER_RUNS_ENDPOINT.format(
                repository=repository,
                workflow=workflow,
                date=controller["created_at"][:10],
            ),
            key="workflow_runs",
        )
        prefix = f"{BUILDER_WORKFLOW_NAME} aa-{controller['id']} "
        discovered = sorted(
            run["id"]
            for run in listing
            if str(run.get("display_title", "")).startswith(prefix)
            and isinstance(run.get("id"), int)
        )
        ledger_ids = sorted(builder["id"] for builder in attempt["builders"])
        if discovered != ledger_ids:
            raise RigInputError(
                f"{name} builder runs differ from GitHub: ledger {ledger_ids}, GitHub {discovered}"
            )
        for builder_index, builder in enumerate(attempt["builders"]):
            builder_name = f"{name}.builders[{builder_index}]"
            expected = {key: value for key, value in builder.items() if key != "jobs"}
            live_builder = _project_github_run(
                _github_object(
                    fetch,
                    GITHUB_RUN_ENDPOINT.format(repository=repository, run_id=builder["id"]),
                )
            )
            _require_github_equal(expected, live_builder, name=builder_name)
            run_count += 1
            live_jobs = sorted(
                (
                    _project_github_job(job)
                    for job in _github_collection(
                        fetch,
                        GITHUB_JOBS_ENDPOINT.format(repository=repository, run_id=builder["id"]),
                        key="jobs",
                    )
                ),
                key=lambda job: str(job.get("id")),
            )
            ledger_jobs = sorted(builder["jobs"], key=lambda job: str(job.get("id")))
            if live_jobs != ledger_jobs:
                raise RigInputError(f"{builder_name} jobs differ from GitHub")
            job_count += len(ledger_jobs)
    return {
        "ledger_provenance": LEDGER_PROVENANCE_VERIFIED,
        "github_verification": {
            "verified_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_count": run_count,
            "job_count": job_count,
            "builder_query": GITHUB_BUILDER_RUNS_ENDPOINT,
        },
    }


def build_rig_blocked_receipt(
    ledger: dict[str, Any],
    *,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seal dispatch exhaustion: five or more controller dispatches, zero completed passes.

    Without ``verification`` (the result of ``verify_dispatch_ledger``) the receipt
    records ``ledger_provenance: unverified`` and no release authority accepts it.
    """
    attempts = validate_dispatch_ledger(ledger)
    if verification is None:
        provenance: dict[str, Any] = {
            "ledger_provenance": LEDGER_PROVENANCE_UNVERIFIED,
            "github_verification": None,
        }
    else:
        _require_exact_keys(
            verification,
            frozenset({"ledger_provenance", "github_verification"}),
            name="ledger verification",
        )
        provenance = dict(verification)
    head_shas: list[str] = []
    for attempt in attempts:
        if attempt["head_sha"] not in head_shas:
            head_shas.append(attempt["head_sha"])
    payload = {
        "schema_version": RIG_BLOCKED_SCHEMA_VERSION,
        "status": "RIG_BLOCKED",
        "blocked_reason": BLOCKED_REASON_DISPATCH_EXHAUSTED,
        "paid_benchmark_allowed": False,
        "score_claim_allowed": False,
        "repository": ledger["repository"],
        "head_branch": TRUSTED_BRANCH,
        "head_shas": head_shas,
        "controller_workflow": CONTROLLER_WORKFLOW_PATH,
        "builder_workflow": BUILDER_WORKFLOW_PATH,
        "builder_arm_id": DISPATCH_BUILDER_ARM_ID,
        "required_attempt_count": EXTENDED_AA_PASS_COUNT,
        "attempt_count": len(attempts),
        "completed_pass_count": 0,
        "first_dispatched_at": attempts[0]["controller"]["created_at"],
        "last_dispatched_at": attempts[-1]["controller"]["created_at"],
        "attempts": attempts,
        "ledger_fetched_at": ledger["fetched_at"],
        "ledger_sha256": canonical_sha256(ledger),
        **provenance,
        "exhaustion_rule": DISPATCH_EXHAUSTION_RULE,
    }
    payload["rig_blocked_receipt_sha256"] = canonical_sha256(payload)
    return validate_rig_blocked_receipt(payload)


def _validate_rig_blocked_run(raw: object, *, name: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is not an object")
    _require_exact_keys(raw, keys, name=name)
    _positive_int(raw.get("run_id"), name=f"{name}.run_id")
    _positive_int(raw.get("run_attempt"), name=f"{name}.run_attempt")
    if raw.get("event") != "workflow_dispatch":
        raise RigInputError(f"{name} was not a workflow dispatch")
    if raw.get("conclusion") not in COMPLETED_RUN_CONCLUSIONS:
        raise RigInputError(f"{name} conclusion is not a completed verdict")
    created = _parse_timestamp(raw.get("created_at"), name=f"{name}.created_at")
    updated = _parse_timestamp(raw.get("updated_at"), name=f"{name}.updated_at")
    if updated < created:
        raise RigInputError(f"{name} finished before it was created")
    _nonempty_string(raw.get("html_url"), name=f"{name}.html_url")
    return dict(raw)


def _validate_rig_blocked_job(raw: object, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is not an object")
    _require_exact_keys(raw, RIG_BLOCKED_JOB_KEYS, name=name)
    _positive_int(raw.get("job_id"), name=f"{name}.job_id")
    if raw.get("conclusion") not in COMPLETED_JOB_CONCLUSIONS:
        raise RigInputError(f"{name} conclusion is not a completed verdict")
    _require_job_interval(raw, name=name)
    _nonempty_string(raw.get("html_url"), name=f"{name}.html_url")
    return dict(raw)


def _validate_rig_blocked_attempt(  # noqa: PLR0912
    raw: object,
    *,
    index: int,
    head_branch: str,
) -> dict[str, Any]:
    name = f"rig-blocked attempts[{index}]"
    if not isinstance(raw, dict):
        raise RigInputError(f"{name} is not an object")
    _require_exact_keys(raw, RIG_BLOCKED_ATTEMPT_KEYS, name=name)
    experiment_id = _nonempty_string(raw.get("experiment_id"), name=f"{name}.experiment_id")
    if not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
        raise RigInputError(f"{name} experiment id is not path-safe")
    head_sha = _git_sha(raw.get("head_sha"), name=f"{name}.head_sha")
    if raw.get("head_branch") != head_branch:
        raise RigInputError(f"{name} branch differs from the receipt branch")
    controller = _validate_rig_blocked_run(
        raw.get("controller"),
        name=f"{name}.controller",
        keys=RIG_BLOCKED_RUN_KEYS,
    )
    if controller["conclusion"] != "success":
        raise RigInputError(f"{name}.controller did not complete its dispatch")
    controller_created = _parse_timestamp(controller["created_at"], name="controller.created_at")
    builders = raw.get("builders")
    if not isinstance(builders, list) or not builders:
        raise RigInputError(f"{name} binds no builder run")
    for builder_index, raw_builder in enumerate(builders):
        builder_name = f"{name}.builders[{builder_index}]"
        builder = _validate_rig_blocked_run(
            raw_builder,
            name=builder_name,
            keys=RIG_BLOCKED_BUILDER_KEYS,
        )
        if builder["head_sha"] != head_sha:
            raise RigInputError(f"{builder_name} head SHA differs from its controller dispatch")
        if _parse_timestamp(builder["created_at"], name="builder.created_at") < controller_created:
            raise RigInputError(f"{builder_name} predates its controller dispatch")
        official_jobs = builder.get("official_jobs")
        if not isinstance(official_jobs, dict) or set(official_jobs) != set(OFFICIAL_JOB_NAMES):
            raise RigInputError(f"{builder_name} official jobs are missing")
        for key in OFFICIAL_JOB_NAMES:
            job = official_jobs[key]
            if job is None:
                if key == "combined_receipt":
                    continue
                raise RigInputError(f"{builder_name} never ran the official {key} Small domain")
            _validate_rig_blocked_job(job, name=f"{builder_name}.official_jobs.{key}")
        if _completed_two_domain_arm(official_jobs):
            raise RigInputError(
                f"{experiment_id} completed a two-domain arm run; "
                "that is A/A data, not dispatch exhaustion"
            )
    return dict(raw)


def validate_rig_blocked_receipt(raw: object) -> dict[str, Any]:  # noqa: PLR0912, PLR0915
    """Re-derive every rig-blocked claim from its bound dispatch rows."""
    if not isinstance(raw, dict) or raw.get("schema_version") != RIG_BLOCKED_SCHEMA_VERSION:
        raise RigInputError("rig-blocked receipt is missing or invalid")
    _require_exact_keys(raw, RIG_BLOCKED_RECEIPT_KEYS, name="rig-blocked receipt")
    unsigned = {key: value for key, value in raw.items() if key != "rig_blocked_receipt_sha256"}
    if raw.get("rig_blocked_receipt_sha256") != canonical_sha256(unsigned):
        raise RigInputError("rig-blocked receipt digest does not bind its content")
    if raw.get("status") != "RIG_BLOCKED" or (
        raw.get("blocked_reason") != BLOCKED_REASON_DISPATCH_EXHAUSTED
    ):
        raise RigInputError("rig-blocked receipt status or reason is invalid")
    if (
        raw.get("paid_benchmark_allowed") is not False
        or raw.get("score_claim_allowed") is not False
    ):
        raise RigInputError("rig-blocked receipt cannot authorize paid work or a score claim")
    if raw.get("repository") != TRUSTED_REPOSITORY:
        raise RigInputError("rig-blocked repository is not the trusted release repository")
    if (
        raw.get("controller_workflow") != CONTROLLER_WORKFLOW_PATH
        or raw.get("builder_workflow") != BUILDER_WORKFLOW_PATH
        or raw.get("builder_arm_id") != DISPATCH_BUILDER_ARM_ID
        or raw.get("exhaustion_rule") != DISPATCH_EXHAUSTION_RULE
    ):
        raise RigInputError("rig-blocked receipt names an unknown workflow contract")
    if raw.get("required_attempt_count") != EXTENDED_AA_PASS_COUNT:
        raise RigInputError("rig-blocked receipt required attempt count is invalid")
    head_branch = _nonempty_string(raw.get("head_branch"), name="rig-blocked head_branch")
    if head_branch != TRUSTED_BRANCH:
        raise RigInputError("rig-blocked receipt is not on the trusted release branch")
    attempts = raw.get("attempts")
    if not isinstance(attempts, list):
        raise RigInputError("rig-blocked attempts is not a list")
    if raw.get("attempt_count") != len(attempts):
        raise RigInputError("rig-blocked attempt count does not match its rows")
    if len(attempts) < EXTENDED_AA_PASS_COUNT:
        raise RigInputError("rig-blocked receipt holds fewer than five controller dispatches")
    if raw.get("completed_pass_count") != 0:
        raise RigInputError("rig-blocked receipt claims a completed pass")
    rows = [
        _validate_rig_blocked_attempt(item, index=index, head_branch=head_branch)
        for index, item in enumerate(attempts)
    ]
    experiment_ids = {row["experiment_id"] for row in rows}
    controller_ids = {row["controller"]["run_id"] for row in rows}
    builder_ids = [builder["run_id"] for row in rows for builder in row["builders"]]
    if len(experiment_ids) != len(rows) or len(controller_ids) != len(rows):
        raise RigInputError("rig-blocked attempts reuse an experiment or controller run")
    if len(set(builder_ids)) != len(builder_ids):
        raise RigInputError("rig-blocked attempts reuse a builder run")
    dispatched = [
        _parse_timestamp(row["controller"]["created_at"], name="controller.created_at")
        for row in rows
    ]
    if any(later <= earlier for earlier, later in pairwise(dispatched)):
        raise RigInputError("rig-blocked attempts are not in dispatch order")
    if raw.get("first_dispatched_at") != rows[0]["controller"]["created_at"] or (
        raw.get("last_dispatched_at") != rows[-1]["controller"]["created_at"]
    ):
        raise RigInputError("rig-blocked dispatch window does not match its rows")
    head_shas: list[str] = []
    for row in rows:
        if row["head_sha"] not in head_shas:
            head_shas.append(row["head_sha"])
    if raw.get("head_shas") != head_shas:
        raise RigInputError("rig-blocked head SHAs do not match its rows")
    fetched_at = _parse_timestamp(
        raw.get("ledger_fetched_at"), name="rig-blocked ledger_fetched_at"
    )
    updated = [
        _parse_timestamp(run["updated_at"], name="run.updated_at")
        for row in rows
        for run in (row["controller"], *row["builders"])
    ]
    if fetched_at < max(updated):
        raise RigInputError("rig-blocked ledger was fetched before its last run finished")
    _sha256_digest(raw.get("ledger_sha256"), name="rig-blocked ledger_sha256")
    _validate_ledger_provenance(raw, rows=rows, fetched_at=fetched_at)
    return dict(raw)


def _validate_ledger_provenance(
    raw: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    fetched_at: datetime,
) -> None:
    provenance = raw.get("ledger_provenance")
    verification = raw.get("github_verification")
    if provenance == LEDGER_PROVENANCE_UNVERIFIED:
        if verification is not None:
            raise RigInputError("unverified rig-blocked receipt carries a GitHub verification")
        return
    if provenance != LEDGER_PROVENANCE_VERIFIED:
        raise RigInputError("rig-blocked ledger provenance is unknown")
    if not isinstance(verification, dict):
        raise RigInputError("verified rig-blocked receipt is missing its GitHub verification")
    _require_exact_keys(verification, GITHUB_VERIFICATION_KEYS, name="GitHub verification")
    verified_at = _parse_timestamp(verification.get("verified_at"), name="verified_at")
    if verified_at < fetched_at:
        raise RigInputError("rig-blocked ledger was verified before it was fetched")
    builder_count = sum(len(row["builders"]) for row in rows)
    if verification.get("run_count") != len(rows) + builder_count:
        raise RigInputError("GitHub verification run count does not match the receipt rows")
    if _positive_int(verification.get("job_count"), name="job_count") < 2 * builder_count:
        raise RigInputError("GitHub verification job count cannot cover both official domains")
    if verification.get("builder_query") != GITHUB_BUILDER_RUNS_ENDPOINT:
        raise RigInputError("GitHub verification used an unknown builder query")


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
    anchor.add_argument("--arm-run", required=True)
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

    rig_blocked = subparsers.add_parser("rig-blocked")
    rig_blocked.add_argument("--ledger", required=True)
    rig_blocked.add_argument("--output", required=True)
    rig_blocked.add_argument(
        "--verify-github",
        action="store_true",
        help="re-fetch every run and job through gh api and require equality before sealing",
    )
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
                load_json(Path(args.arm_run)),
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
        elif args.command == "rig-blocked":
            ledger = load_json(Path(args.ledger))
            verification = (
                verify_dispatch_ledger(ledger, fetch=gh_api_fetch) if args.verify_github else None
            )
            payload = build_rig_blocked_receipt(ledger, verification=verification)
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
