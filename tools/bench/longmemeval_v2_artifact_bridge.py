#!/usr/bin/env python3
"""Convert official LongMemEval-V2 artifacts into signed rig contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bench import longmemeval_v2_rig as rig  # noqa: E402

OFFICIAL_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-official-plan-v2"
OFFICIAL_RECEIPT_SCHEMA_VERSION = "sibyl-longmemeval-v2-official-receipt-v1"
PROVIDER_USAGE_SCHEMA_VERSION = "sibyl-provider-usage-event-v1"
EXPERIMENT_IDENTITY_SCHEMA_VERSION = "sibyl-longmemeval-v2-experiment-identity-v2"
SPEND_RESERVATION_SCHEMA_VERSION = "sibyl-longmemeval-v2-spend-reservation-v1"
SHA256_HEX_LENGTH = 64

OFFICIAL_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "provider_usage_run_id",
        "experiment_identity_schema_version",
        "runner_provenance",
        "execution",
        "experiment_id",
        "experiment_phase",
        "pass_id",
        "pass_seed",
        "arm_role",
        "substrate",
        "preregistration_sha256",
        "max_spend_usd",
        "domain",
        "tier",
        "method",
        "data_root",
        "output_dir",
        "runtime_dir",
        "memory_config_path",
        "official_repo",
        "official_source",
        "plan_only",
        "save_memory",
        "skip_evaluation",
        "load_memory_dir",
        "checkpoint_dir",
        "reuse_existing_project",
        "trajectory_path",
        "trajectory_path_exists",
        "question_count",
        "selected_question_ids_sha256",
        "official_question_count",
        "official_question_ids_sha256",
        "selection_complete",
        "required_trajectory_count",
        "llm_eval_count",
        "spend_reservation",
        "reader_model",
        "reader_base_url",
        "reader_max_concurrent_requests",
        "reader_retry_attempts",
        "reader_retry_base_delay_seconds",
        "reader_retry_max_delay_seconds",
        "memory_api_timeout_seconds",
        "memory_api_retry_attempts",
        "memory_api_retry_base_delay_seconds",
        "memory_api_retry_max_delay_seconds",
        "chunking_mode",
        "max_chunks_per_trajectory",
        "neighbor_stitch_items",
        "neighbor_stitch_span",
        "neighbor_support_exempt",
        "neighbor_trajectory_preserving",
        "neighbor_support_overflow_items",
        "neighbor_stitch_spread",
        "semantic_prior_rescue_weight",
        "typed_pool",
        "context_expansion_max_ratio",
        "max_context_total_chars",
        "operational_note_dedupe_mode",
        "operational_note_lane_mode",
        "operational_note_distillation_profile",
        "render_char_total_treatment",
        "render_group_lanes",
        "render_action_spines",
        "evidence_types",
        "evidence_char_budget",
        "evidence_char_budget_raw_reserve",
        "evidence_composition_mode",
        "source_evidence_bundling",
        "typed_stream_retrieval",
        "typed_stream_limit",
        "note_distillation",
        "note_distillation_model",
        "typed_reservation_items",
        "retrieval_mode",
        "knn_type_overfetch",
        "retrieval_max_planned_queries",
        "agentic_traversal",
        "traversal_widening_rounds",
        "traversal_model",
        "traversal_max_actions",
        "traversal_followup_searches",
        "traversal_deadline_seconds",
        "traversal_overflow_items",
        "traversal_search_limit",
        "include_screenshot_refs",
        "evaluator_model",
        "evaluator_retry_attempts",
        "provider_usage",
        "requirements",
        "summary",
        "honesty_contract",
    }
)
OFFICIAL_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "suite",
        "suite_version",
        "generated_at",
        "sibyl_commit",
        "runner_provenance",
        "command",
        "domain",
        "tier",
        "method",
        "claim_boundary",
        "official_repo",
        "dataset",
        "source_runs",
        "models",
        "artifacts",
        "metrics",
        "accounting",
        "approval_boundary",
        "checks",
    }
)
SOURCE_RUNS_KEYS = frozenset(
    {
        "expected_domains",
        "domains",
        "complete",
        "integrity_complete",
        "api_runtime_consistent",
        "model_consistent",
        "method_consistent",
    }
)
DOMAIN_SOURCE_KEYS = frozenset(
    {
        "output_dir",
        "plan",
        "official_receipt",
        "run_args",
        "aggregated_metrics",
        "per_question",
        "rig_rows",
        "runtime_inputs",
        "provider_usage",
        "effective_memory_config",
        "api_runtime",
        "api_runtime_consistent",
        "reader_model",
        "reader_base_url",
        "evaluator_model",
        "method",
        "tier",
    }
)
ARTIFACT_RECORD_KEYS = frozenset({"path", "exists", "sha256", "size_bytes"})
PROVIDER_RECORD_KEYS = frozenset(
    ARTIFACT_RECORD_KEYS
    | {
        "event_count",
        "invalid_line_count",
        "run_ids",
        "expected_run_id",
        "foreign_event_count",
        "attempt_count",
    }
)
RUNTIME_INPUT_KEYS = frozenset({"questions", "haystack", "memory_config"})
PROVIDER_ROLE_KEYS = frozenset({"reader", "judge"})
PROVENANCE_KEYS = frozenset({"sibyl_commit", "git_dirty", "git_status"})
OFFICIAL_DATASET_KEYS = frozenset(
    {
        "name",
        "data_root",
        "tier",
        "questions_sha256",
        "trajectories_sha256",
        "haystack_sha256",
        "question_count",
        "selected_question_ids_sha256",
        "official_question_count",
        "official_question_ids_sha256",
        "selection_complete",
        "required_trajectory_count",
    }
)
OFFICIAL_MODEL_KEYS = frozenset(
    {
        "reader_model",
        "reader_base_url",
        "reader_expected_fragment",
        "evaluator_model",
        "evaluator_expected_fragment",
        "evaluator_reasoning_effort",
    }
)
SPEND_RESERVATION_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "currency",
        "price_snapshot",
        "price_snapshot_sources",
        "sections",
        "metered_estimate_usd",
        "contingency_multiplier",
        "unmetered_provider_reserve_usd",
        "reserved_total_usd",
        "max_spend_usd",
        "within_cap",
        "enforcement",
    }
)
RIG_ROW_KEYS = frozenset(
    {
        "question_id",
        "status",
        "context_status",
        "evidence_exposure_eligible",
        "evidence_exposed",
        "activity",
    }
)
PER_QUESTION_KEYS = frozenset(
    {
        "index",
        "stream_index",
        "question_id",
        "question_type",
        "category",
        "is_abstention_problem",
        "eval_function",
        "question_text",
        "question_image",
        "haystack_ids",
        "memory_context",
        "memory_context_original_token_count",
        "memory_context_token_count",
        "memory_context_was_truncated",
        "memory_query_duration_seconds",
        "memory_post_query_duration_seconds",
        "memory_post_query_metadata",
        "prompt_messages",
        "answer_gold",
        "response_raw",
        "response_parsed_boxed",
        "is_unknown",
        "score",
        "score_bool",
        "usage",
        "timestamp_utc",
    }
)
RUNTIME_QUESTION_KEYS = frozenset(
    {"id", "question", "answer", "question_type", "eval_function", "domain", "environment"}
)
RUN_ARGS_REQUIRED_KEYS = frozenset(
    {
        "domain",
        "questions_path",
        "haystack_path",
        "memory_config_path",
        "output_dir",
        "model",
        "base_url",
        "evaluator_model",
        "shuffle_questions_seed",
    }
)
CHECK_KEYS = frozenset({"name", "status", "detail", "surfaces"})
REQUIRED_PASSING_CHECKS = frozenset(
    {
        "official harness",
        "dataset hashes",
        "model pins",
        "source runs",
        "runtime provenance",
        "accounting",
        "approval boundary",
    }
)
CONFIG_EXCLUDED_KEYS = frozenset(
    {
        "api_url",
        "longmemeval_v2_domain",
        "project_id",
        "run_id",
        "runner_provenance",
        *rig.GEOMETRY_KEYS,
    }
)
PLAN_PROFILE_KEYS = frozenset(
    {
        "operational_note_dedupe_mode",
        "operational_note_lane_mode",
        "operational_note_distillation_profile",
        "render_char_total_treatment",
        "render_group_lanes",
        "render_action_spines",
    }
)
SOURCE_ARTIFACT_NAMES = frozenset(
    {
        "combined_receipt",
        "plan",
        "official_receipt",
        "run_args",
        "aggregated_metrics",
        "per_question",
        "rig_rows",
        "runtime_questions",
        "runtime_haystack",
        "runtime_memory_config",
        "reader_provider_usage",
        "judge_provider_usage",
    }
)


class BridgeInputError(rig.RigInputError):
    """Raised when official artifacts cannot support a signed rig contract."""


def _require_exact_keys(raw: dict[str, Any], expected: frozenset[str], *, name: str) -> None:
    actual = set(raw)
    if actual != expected:
        raise BridgeInputError(
            f"{name} fields differ: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )


def _nonempty_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeInputError(f"{name} must be a non-empty string")
    return value.strip()


def _nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BridgeInputError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: object, *, name: str) -> int:
    result = _nonnegative_int(value, name=name)
    if result == 0:
        raise BridgeInputError(f"{name} must be positive")
    return result


def _finite_number(value: object, *, name: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BridgeInputError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise BridgeInputError(f"{name} is outside its allowed range")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise BridgeInputError(f"{path} is not a JSON object")
    return raw


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise BridgeInputError(f"{path}:{line_number} is blank")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise BridgeInputError(f"{path}:{line_number} is not a JSON object")
        rows.append(row)
    if not rows:
        raise BridgeInputError(f"{path} is empty")
    return rows


def _artifact_path(
    raw: object,
    *,
    expected_path: Path,
    name: str,
    provider: bool = False,
) -> tuple[Path, str]:
    if not isinstance(raw, dict):
        raise BridgeInputError(f"{name} artifact record is missing")
    _require_exact_keys(
        raw,
        PROVIDER_RECORD_KEYS if provider else ARTIFACT_RECORD_KEYS,
        name=f"{name} artifact",
    )
    path = Path(_nonempty_string(raw.get("path"), name=f"{name}.path")).resolve()
    if path != expected_path.resolve():
        raise BridgeInputError(f"{name} path does not match its official output location")
    if raw.get("exists") is not True or not path.is_file():
        raise BridgeInputError(f"{name} artifact is missing")
    size = _nonnegative_int(raw.get("size_bytes"), name=f"{name}.size_bytes")
    if path.stat().st_size != size:
        raise BridgeInputError(f"{name} artifact size does not match its receipt")
    expected_digest = rig._sha256_digest(raw.get("sha256"), name=f"{name}.sha256")
    if _sha256_file(path) != expected_digest:
        raise BridgeInputError(f"{name} artifact digest does not match its bytes")
    return path, expected_digest


def _validate_checks(raw: object, *, name: str) -> None:
    if not isinstance(raw, list):
        raise BridgeInputError(f"{name}.checks is missing")
    statuses: dict[str, str] = {}
    for index, check in enumerate(raw):
        if not isinstance(check, dict):
            raise BridgeInputError(f"{name}.checks[{index}] is invalid")
        _require_exact_keys(check, CHECK_KEYS, name=f"{name}.checks[{index}]")
        check_name = _nonempty_string(check.get("name"), name=f"{name}.checks[{index}].name")
        if check_name in statuses:
            raise BridgeInputError(f"{name} repeats check {check_name!r}")
        statuses[check_name] = _nonempty_string(
            check.get("status"),
            name=f"{name}.checks[{index}].status",
        )
    missing = REQUIRED_PASSING_CHECKS - statuses.keys()
    failed = sorted(check for check in REQUIRED_PASSING_CHECKS if statuses.get(check) != "PASS")
    if missing or failed:
        raise BridgeInputError(
            f"{name} required checks are not PASS: missing={sorted(missing)}, failed={failed}"
        )


def _validate_receipt(raw: object, *, name: str, domain: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BridgeInputError(f"{name} is not an object")
    _require_exact_keys(raw, OFFICIAL_RECEIPT_KEYS, name=name)
    if raw.get("schema_version") != OFFICIAL_RECEIPT_SCHEMA_VERSION:
        raise BridgeInputError(f"{name} schema is invalid")
    if raw.get("domain") != domain or raw.get("tier") != "small":
        raise BridgeInputError(f"{name} domain or tier is invalid")
    _validate_checks(raw.get("checks"), name=name)
    return dict(raw)


def _validate_source_runs(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BridgeInputError("combined receipt source_runs is missing")
    _require_exact_keys(raw, SOURCE_RUNS_KEYS, name="combined receipt source_runs")
    if raw.get("expected_domains") != ["web", "enterprise"]:
        raise BridgeInputError("combined receipt expected domains are not exact")
    for flag in (
        "complete",
        "integrity_complete",
        "api_runtime_consistent",
        "model_consistent",
        "method_consistent",
    ):
        if raw.get(flag) is not True:
            raise BridgeInputError(f"combined receipt source_runs.{flag} is not true")
    domains = raw.get("domains")
    if not isinstance(domains, dict) or set(domains) != rig.DOMAINS:
        raise BridgeInputError("combined receipt source domains are not exact")
    return dict(raw)


def _validate_plan(  # noqa: PLR0912, PLR0915
    raw: object,
    *,
    domain: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BridgeInputError(f"{domain} plan is not an object")
    _require_exact_keys(raw, OFFICIAL_PLAN_KEYS, name=f"{domain} plan")
    if raw.get("schema_version") != OFFICIAL_PLAN_SCHEMA_VERSION:
        raise BridgeInputError(f"{domain} plan schema is invalid")
    if raw.get("experiment_identity_schema_version") != EXPERIMENT_IDENTITY_SCHEMA_VERSION:
        raise BridgeInputError(f"{domain} experiment identity schema is invalid")
    if raw.get("domain") != domain or raw.get("tier") != "small":
        raise BridgeInputError(f"{domain} plan domain or tier is invalid")
    if (
        raw.get("plan_only") is not False
        or raw.get("skip_evaluation") is not False
        or raw.get("trajectory_path_exists") is not True
    ):
        raise BridgeInputError(f"{domain} plan is not a completed scored run")
    question_count = _positive_int(raw.get("question_count"), name=f"{domain} plan.question_count")
    official_question_count = _positive_int(
        raw.get("official_question_count"),
        name=f"{domain} plan.official_question_count",
    )
    if (
        raw.get("selection_complete") is not True
        or question_count != official_question_count
        or raw.get("selected_question_ids_sha256") != raw.get("official_question_ids_sha256")
    ):
        raise BridgeInputError(f"{domain} plan does not cover the complete Small corpus")
    if (
        official_question_count != rig.OFFICIAL_SMALL_QUESTION_COUNTS[domain]
        or raw.get("official_question_ids_sha256") != rig.OFFICIAL_SMALL_QUESTION_IDS_SHA256[domain]
    ):
        raise BridgeInputError(f"{domain} plan does not match the pinned Small corpus")
    _nonnegative_int(raw.get("pass_seed"), name=f"{domain} plan.pass_seed")
    maximum_spend = _finite_number(
        raw.get("max_spend_usd"),
        name=f"{domain} plan.max_spend_usd",
    )
    reservation = raw.get("spend_reservation")
    if not isinstance(reservation, dict):
        raise BridgeInputError(f"{domain} spend reservation is missing")
    _require_exact_keys(
        reservation,
        SPEND_RESERVATION_KEYS,
        name=f"{domain} spend reservation",
    )
    if (
        reservation.get("schema_version") != SPEND_RESERVATION_SCHEMA_VERSION
        or reservation.get("status") != "PASS"
        or reservation.get("currency") != "USD"
        or reservation.get("within_cap") is not True
        or reservation.get("max_spend_usd") != maximum_spend
        or _finite_number(
            reservation.get("reserved_total_usd"),
            name=f"{domain} reserved spend",
        )
        > maximum_spend
    ):
        raise BridgeInputError(f"{domain} spend reservation is not within its fixed cap")
    for field in (
        "run_id",
        "provider_usage_run_id",
        "experiment_id",
        "experiment_phase",
        "pass_id",
        "arm_role",
        "reader_model",
        "reader_base_url",
        "evaluator_model",
        "data_root",
        "official_repo",
    ):
        _nonempty_string(raw.get(field), name=f"{domain} plan.{field}")
    _positive_int(
        raw.get("required_trajectory_count"),
        name=f"{domain} plan.required_trajectory_count",
    )
    phase = _nonempty_string(raw.get("experiment_phase"), name=f"{domain} plan phase")
    if phase not in rig.EXPERIMENT_PHASES:
        raise BridgeInputError(f"{domain} plan experiment phase is invalid")
    if phase in {"aa", "anchor"}:
        if raw.get("preregistration_sha256") is not None:
            raise BridgeInputError(f"{domain} {phase} plan cannot bind preregistration")
        normalized_preregistration = ""
    else:
        preregistration = _nonempty_string(
            raw.get("preregistration_sha256"),
            name=f"{domain} plan.preregistration_sha256",
        )
        if len(preregistration) != SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in preregistration
        ):
            raise BridgeInputError(
                f"{domain} plan.preregistration_sha256 is not a lowercase SHA-256 digest"
            )
        normalized_preregistration = f"sha256:{preregistration}"
    if raw.get("substrate") not in {"machine", "naive"}:
        raise BridgeInputError(f"{domain} plan substrate is invalid")
    requirements = raw.get("requirements")
    if (
        not isinstance(requirements, dict)
        or not requirements
        or any(value is not True for value in requirements.values())
    ):
        raise BridgeInputError(f"{domain} plan requirements are not all satisfied")
    execution = rig.validate_execution_identity(
        raw.get("execution"),
        name=f"{domain} execution",
    )
    provenance = raw.get("runner_provenance")
    if not isinstance(provenance, dict):
        raise BridgeInputError(f"{domain} runner provenance is missing")
    _require_exact_keys(provenance, PROVENANCE_KEYS, name=f"{domain} runner provenance")
    if (
        provenance.get("sibyl_commit") != execution["sha"]
        or provenance.get("git_dirty") is not False
        or provenance.get("git_status") != "clean"
    ):
        raise BridgeInputError(f"{domain} runner provenance is not the clean execution SHA")
    official_source = raw.get("official_source")
    if not isinstance(official_source, dict):
        raise BridgeInputError(f"{domain} official source is missing")
    source_path = Path(
        _nonempty_string(official_source.get("path"), name=f"{domain} official source path")
    ).resolve()
    if Path(str(raw["official_repo"])).resolve() != source_path:
        raise BridgeInputError(f"{domain} official source path differs from its plan")
    return {
        **raw,
        "execution": execution,
        "preregistration_sha256": normalized_preregistration,
    }


def _validate_runtime(
    raw: object,
    *,
    execution_sha: str,
    name: str,
) -> None:
    if not isinstance(raw, dict) or set(raw) != {"status", "version", "runtime"}:
        raise BridgeInputError(f"{name} API runtime is invalid")
    runtime = raw.get("runtime")
    if not isinstance(runtime, dict):
        raise BridgeInputError(f"{name} API runtime identity is missing")
    if (
        raw.get("status") != "healthy"
        or runtime.get("commit") != execution_sha
        or runtime.get("git_dirty") is not False
        or runtime.get("git_status") != "clean"
    ):
        raise BridgeInputError(f"{name} API runtime does not match the clean execution SHA")


def _configuration_and_geometry(
    raw: object,
    *,
    plan: dict[str, Any],
    domain: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    if not isinstance(raw, dict) or set(raw) != {"memory_type", "memory_params"}:
        raise BridgeInputError(f"{domain} effective memory config is invalid")
    if raw.get("memory_type") != "sibyl_live_api":
        raise BridgeInputError(f"{domain} memory type is not Sibyl live API")
    params = raw.get("memory_params")
    if not isinstance(params, dict):
        raise BridgeInputError(f"{domain} memory params are missing")
    geometry = rig._validate_geometry(
        {key: params.get(key) for key in rig.GEOMETRY_KEYS},
        name=f"{domain} geometry",
    )
    configuration = {key: value for key, value in params.items() if key not in CONFIG_EXCLUDED_KEYS}
    for key in PLAN_PROFILE_KEYS:
        if key in configuration and configuration[key] != plan[key]:
            raise BridgeInputError(f"{domain} plan and effective config differ for {key}")
        configuration[key] = plan[key]
    if not configuration:
        raise BridgeInputError(f"{domain} normalized configuration is empty")
    for key, value in configuration.items():
        if key in plan and plan[key] != value:
            raise BridgeInputError(f"{domain} plan and effective config differ for {key}")
    if plan.get("max_context_total_chars") != geometry["max_context_total_chars"]:
        raise BridgeInputError(f"{domain} plan and effective geometry differ")
    return configuration, geometry


def _question_ids_sha256(question_ids: list[str]) -> str:
    encoded = json.dumps(sorted(question_ids), separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _provider_record(
    raw: object,
    *,
    expected_path: Path,
    expected_run_id: str,
    name: str,
) -> tuple[int, str]:
    _path, digest = _artifact_path(
        raw,
        expected_path=expected_path,
        name=name,
        provider=True,
    )
    assert isinstance(raw, dict)
    event_count = _positive_int(raw.get("event_count"), name=f"{name}.event_count")
    if (
        raw.get("invalid_line_count") != 0
        or raw.get("foreign_event_count") != 0
        or raw.get("attempt_count") != 1
        or raw.get("expected_run_id") != expected_run_id
        or raw.get("run_ids") != [expected_run_id]
    ):
        raise BridgeInputError(f"{name} provider run lineage is incomplete or foreign")
    return event_count, digest


def _domain_cost_and_tokens(receipt: dict[str, Any], *, domain: str) -> tuple[float, int]:
    accounting = receipt.get("accounting")
    if not isinstance(accounting, dict):
        raise BridgeInputError(f"{domain} receipt accounting is missing")
    cost = accounting.get("cost")
    if not isinstance(cost, dict) or cost.get("coverage_complete") is not True:
        raise BridgeInputError(f"{domain} provider cost coverage is incomplete")
    actual_cost = _finite_number(
        cost.get("provider_reported_total_usd"),
        name=f"{domain} provider cost",
    )
    total_tokens = 0
    for role in PROVIDER_ROLE_KEYS:
        section = accounting.get(role)
        if not isinstance(section, dict) or section.get("tracking_complete") is not True:
            raise BridgeInputError(f"{domain} {role} accounting is incomplete")
        for field in ("estimated_input_tokens", "estimated_output_tokens"):
            value = _finite_number(section.get(field), name=f"{domain} {role}.{field}")
            if not value.is_integer():
                raise BridgeInputError(f"{domain} {role}.{field} is not integral")
            total_tokens += int(value)
    return actual_cost, total_tokens


def _validate_dataset_and_models(
    receipt: dict[str, Any],
    *,
    plan: dict[str, Any],
    domain: str,
) -> dict[str, str]:
    dataset = receipt.get("dataset")
    if not isinstance(dataset, dict):
        raise BridgeInputError(f"{domain} dataset receipt is missing")
    _require_exact_keys(dataset, OFFICIAL_DATASET_KEYS, name=f"{domain} dataset")
    if (
        dataset.get("name") != "longmemeval-v2"
        or dataset.get("tier") != "small"
        or Path(str(dataset.get("data_root"))).resolve() != Path(str(plan["data_root"])).resolve()
        or dataset.get("question_count") != plan["question_count"]
        or dataset.get("selected_question_ids_sha256") != plan["selected_question_ids_sha256"]
        or dataset.get("official_question_count") != plan["official_question_count"]
        or dataset.get("official_question_ids_sha256") != plan["official_question_ids_sha256"]
        or dataset.get("selection_complete") is not True
        or dataset.get("required_trajectory_count") != plan["required_trajectory_count"]
    ):
        raise BridgeInputError(f"{domain} dataset identity differs from its plan")
    dataset_hashes = {
        field: rig._sha256_digest(dataset.get(field), name=f"{domain} dataset.{field}")
        for field in ("questions_sha256", "trajectories_sha256", "haystack_sha256")
    }
    models = receipt.get("models")
    if not isinstance(models, dict):
        raise BridgeInputError(f"{domain} model receipt is missing")
    _require_exact_keys(models, OFFICIAL_MODEL_KEYS, name=f"{domain} models")
    if (
        models.get("reader_model") != plan["reader_model"]
        or models.get("reader_base_url") != plan["reader_base_url"]
        or models.get("evaluator_model") != plan["evaluator_model"]
    ):
        raise BridgeInputError(f"{domain} receipt models differ from its plan")
    for field in (
        "reader_expected_fragment",
        "evaluator_expected_fragment",
        "evaluator_reasoning_effort",
    ):
        _nonempty_string(models.get(field), name=f"{domain} models.{field}")
    return dataset_hashes


def _build_rows(  # noqa: PLR0912, PLR0915
    *,
    domain: str,
    plan: dict[str, Any],
    configuration: dict[str, Any],
    stack_digest: str,
    per_question: list[dict[str, Any]],
    rig_rows: list[dict[str, Any]],
    runtime_questions: object,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not isinstance(runtime_questions, list) or not runtime_questions:
        raise BridgeInputError(f"{domain} runtime questions are empty")
    runtime_ids: list[str] = []
    for index, question in enumerate(runtime_questions):
        if not isinstance(question, dict):
            raise BridgeInputError(f"{domain} runtime questions[{index}] is invalid")
        _require_exact_keys(
            question,
            RUNTIME_QUESTION_KEYS,
            name=f"{domain} runtime questions[{index}]",
        )
        runtime_ids.append(
            _nonempty_string(question.get("id"), name=f"{domain} runtime question id")
        )
    question_count = int(plan["question_count"])
    if len(runtime_ids) != question_count:
        raise BridgeInputError(f"{domain} runtime question count differs from the plan")
    if len(set(runtime_ids)) != len(runtime_ids):
        raise BridgeInputError(f"{domain} runtime question IDs are not unique")
    if plan.get("selected_question_ids_sha256") != _question_ids_sha256(runtime_ids):
        raise BridgeInputError(f"{domain} selected question digest is invalid")
    expected_order = list(runtime_ids)
    random.Random(int(plan["pass_seed"])).shuffle(expected_order)  # noqa: S311
    if len(per_question) != question_count or len(rig_rows) != question_count:
        raise BridgeInputError(f"{domain} result row counts differ from the plan")
    actual_order: list[str] = []
    rows: list[dict[str, Any]] = []
    lever_totals: dict[str, int] = {}
    mode = _nonempty_string(configuration.get("retrieval_mode"), name="retrieval mode")
    for index, (score_row, trace_row) in enumerate(zip(per_question, rig_rows, strict=True)):
        _require_exact_keys(score_row, PER_QUESTION_KEYS, name=f"{domain} scores[{index}]")
        _require_exact_keys(trace_row, RIG_ROW_KEYS, name=f"{domain} rig_rows[{index}]")
        question_id = _nonempty_string(
            score_row.get("question_id"),
            name=f"{domain} scores[{index}].question_id",
        )
        if trace_row.get("question_id") != question_id:
            raise BridgeInputError(f"{domain} rig row order differs from official scores")
        if score_row.get("stream_index") != index:
            raise BridgeInputError(f"{domain} stream indexes are not contiguous")
        original_index = _nonnegative_int(
            score_row.get("index"),
            name=f"{domain} scores[{index}].index",
        )
        if original_index >= len(runtime_ids) or runtime_ids[original_index] != question_id:
            raise BridgeInputError(f"{domain} score row does not bind its runtime question")
        if trace_row.get("status") != "valid":
            raise BridgeInputError(f"{domain} rig row {question_id} failed")
        if not isinstance(score_row.get("score_bool"), bool):
            raise BridgeInputError(f"{domain} score row {question_id} is incomplete")
        latency = sum(
            _finite_number(score_row.get(field), name=f"{domain} {question_id}.{field}")
            for field in (
                "memory_query_duration_seconds",
                "memory_post_query_duration_seconds",
            )
        )
        usage = score_row.get("usage")
        if not isinstance(usage, dict):
            raise BridgeInputError(f"{domain} score row {question_id} has no reader usage")
        reader_tokens = _positive_int(
            usage.get("total_tokens"),
            name=f"{domain} score row {question_id}.total_tokens",
        )
        context = score_row.get("memory_context")
        if not isinstance(context, list):
            raise BridgeInputError(f"{domain} score row {question_id} context is invalid")
        expected_context_status = "complete" if context else "empty"
        if trace_row.get("context_status") != expected_context_status:
            raise BridgeInputError(f"{domain} rig row {question_id} context status drifted")
        activity = rig._validate_activity(
            trace_row.get("activity"),
            mode=mode,
            name=f"{domain} rig row {question_id}",
        )
        for lever, count in activity["lever_activity"].items():
            lever_totals[lever] = lever_totals.get(lever, 0) + count
        rows.append(
            {
                "status": "valid",
                "domain": domain,
                "question_id": question_id,
                "score_bool": score_row["score_bool"],
                "latency_seconds": latency,
                "reader_tokens": reader_tokens,
                "evidence_exposure_eligible": trace_row["evidence_exposure_eligible"],
                "evidence_exposed": trace_row["evidence_exposed"],
                "context_status": trace_row["context_status"],
                "stack_fingerprint": stack_digest,
                "activity": activity,
            }
        )
        actual_order.append(question_id)
    if actual_order != expected_order:
        raise BridgeInputError(f"{domain} question order does not match its pass seed")
    return rows, lever_totals


def _validate_cross_domain_identity(domain_runs: dict[str, dict[str, Any]]) -> None:
    web = domain_runs["web"]
    enterprise = domain_runs["enterprise"]
    for field in (
        "experiment_id",
        "experiment_phase",
        "pass_id",
        "pass_seed",
        "arm_role",
        "substrate",
        "preregistration_sha256",
        "max_spend_usd",
        "execution",
        "configuration",
        "geometry",
        "reader",
        "judge",
        "official_source",
    ):
        if web[field] != enterprise[field]:
            raise BridgeInputError(f"cross-domain identity differs for {field}")
    if web["provider_usage_run_id"] == enterprise["provider_usage_run_id"]:
        raise BridgeInputError("provider usage run IDs must be unique per domain invocation")


def _domain_artifacts(  # noqa: PLR0912, PLR0915
    *,
    domain: str,
    source: object,
    combined_receipt_path: Path,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise BridgeInputError(f"{domain} source-run record is missing")
    _require_exact_keys(source, DOMAIN_SOURCE_KEYS, name=f"{domain} source run")
    output_dir = Path(
        _nonempty_string(source.get("output_dir"), name=f"{domain} output_dir")
    ).resolve()
    expected = {
        "plan": output_dir / "longmemeval_v2_official_plan.json",
        "official_receipt": output_dir / "longmemeval_v2_official_receipt.json",
        "run_args": output_dir / "run_args.json",
        "aggregated_metrics": output_dir / "aggregated_metrics.json",
        "per_question": output_dir / "per_question.jsonl",
        "rig_rows": output_dir / "rig_rows.jsonl",
    }
    paths: dict[str, Path] = {}
    digests: dict[str, str] = {"combined_receipt": _sha256_file(combined_receipt_path)}
    for artifact, expected_path in expected.items():
        paths[artifact], digests[artifact] = _artifact_path(
            source.get(artifact),
            expected_path=expected_path,
            name=f"{domain}.{artifact}",
        )
    runtime = source.get("runtime_inputs")
    if not isinstance(runtime, dict):
        raise BridgeInputError(f"{domain} runtime input records are missing")
    _require_exact_keys(runtime, RUNTIME_INPUT_KEYS, name=f"{domain} runtime inputs")
    for artifact, filename in (
        ("questions", "questions.json"),
        ("haystack", "haystack.json"),
        ("memory_config", "memory_config.json"),
    ):
        path, digest = _artifact_path(
            runtime.get(artifact),
            expected_path=output_dir / "runtime_inputs" / filename,
            name=f"{domain}.runtime_{artifact}",
        )
        paths[f"runtime_{artifact}"] = path
        digests[f"runtime_{artifact}"] = digest
    plan = _validate_plan(_load_json(paths["plan"]), domain=domain)
    if Path(str(plan["output_dir"])).resolve() != output_dir:
        raise BridgeInputError(f"{domain} plan output directory drifted")
    if Path(str(plan["runtime_dir"])).resolve() != (output_dir / "runtime_inputs").resolve():
        raise BridgeInputError(f"{domain} plan runtime directory drifted")
    if Path(str(plan["memory_config_path"])).resolve() != paths["runtime_memory_config"]:
        raise BridgeInputError(f"{domain} plan memory config path drifted")
    provider_usage = source.get("provider_usage")
    if not isinstance(provider_usage, dict):
        raise BridgeInputError(f"{domain} provider usage records are missing")
    _require_exact_keys(provider_usage, PROVIDER_ROLE_KEYS, name=f"{domain} provider usage")
    provider_requests = 0
    for role in sorted(PROVIDER_ROLE_KEYS):
        expected_path = output_dir / "provider_usage" / f"{role}.jsonl"
        event_count, digest = _provider_record(
            provider_usage.get(role),
            expected_path=expected_path,
            expected_run_id=str(plan["provider_usage_run_id"]),
            name=f"{domain}.{role}_provider_usage",
        )
        provider_requests += event_count
        digests[f"{role}_provider_usage"] = digest
    if plan.get("provider_usage") != {
        role: str(output_dir / "provider_usage" / f"{role}.jsonl")
        for role in sorted(PROVIDER_ROLE_KEYS)
    }:
        raise BridgeInputError(f"{domain} plan provider usage paths drifted")
    run_args = _load_json(paths["run_args"])
    if not RUN_ARGS_REQUIRED_KEYS.issubset(run_args):
        raise BridgeInputError(f"{domain} run args omit required keys")
    if (
        run_args.get("domain") != domain
        or Path(str(run_args.get("questions_path"))).resolve() != paths["runtime_questions"]
        or Path(str(run_args.get("haystack_path"))).resolve() != paths["runtime_haystack"]
        or Path(str(run_args.get("memory_config_path"))).resolve() != paths["runtime_memory_config"]
        or Path(str(run_args.get("output_dir"))).resolve() != output_dir
        or run_args.get("model") != plan["reader_model"]
        or run_args.get("base_url") != plan["reader_base_url"]
        or run_args.get("evaluator_model") != plan["evaluator_model"]
        or run_args.get("shuffle_questions_seed") != plan["pass_seed"]
    ):
        raise BridgeInputError(f"{domain} run args differ from the official plan")
    official_receipt = _validate_receipt(
        _load_json(paths["official_receipt"]),
        name=f"{domain} official receipt",
        domain=domain,
    )
    if (
        official_receipt.get("sibyl_commit") != plan["execution"]["sha"]
        or official_receipt.get("official_repo") != plan["official_source"]
        or official_receipt.get("runner_provenance") != plan["runner_provenance"]
        or official_receipt.get("method") != plan["method"]
    ):
        raise BridgeInputError(f"{domain} official receipt stack differs from its plan")
    actual_cost, total_tokens = _domain_cost_and_tokens(official_receipt, domain=domain)
    if actual_cost > float(plan["max_spend_usd"]):
        raise BridgeInputError(f"{domain} provider cost exceeds its approved spend")
    if source.get("api_runtime_consistent") is not True:
        raise BridgeInputError(f"{domain} API runtime is inconsistent")
    _validate_runtime(
        source.get("api_runtime"),
        execution_sha=str(plan["execution"]["sha"]),
        name=domain,
    )
    configuration, geometry = _configuration_and_geometry(
        source.get("effective_memory_config"),
        plan=plan,
        domain=domain,
    )
    if (
        source.get("reader_model") != plan["reader_model"]
        or source.get("reader_base_url") != plan["reader_base_url"]
        or source.get("evaluator_model") != plan["evaluator_model"]
        or source.get("method") != plan["method"]
        or source.get("tier") != plan["tier"]
    ):
        raise BridgeInputError(f"{domain} source-run identity differs from its plan")
    dataset_hashes = _validate_dataset_and_models(
        official_receipt,
        plan=plan,
        domain=domain,
    )
    dataset_digest = rig.canonical_sha256(
        {
            **dataset_hashes,
            "runtime_questions_sha256": digests["runtime_questions"],
            "runtime_haystack_sha256": digests["runtime_haystack"],
            "selected_question_ids_sha256": plan["selected_question_ids_sha256"],
        }
    )
    stack = {
        "sibyl_commit": plan["execution"]["sha"],
        "sibyl_git_status": "clean",
        "official_source": plan["official_source"],
        "dataset_sha256_by_domain": {domain: dataset_digest},
        "reader": {"model": plan["reader_model"], "base_url": plan["reader_base_url"]},
        "judge": {"model": plan["evaluator_model"]},
    }
    return {
        **plan,
        "configuration": configuration,
        "geometry": geometry,
        "reader": stack["reader"],
        "judge": stack["judge"],
        "official_source": stack["official_source"],
        "dataset_digest": dataset_digest,
        "dataset_hashes": dataset_hashes,
        "stack_fragment": stack,
        "per_question": _load_jsonl(paths["per_question"]),
        "rig_rows": _load_jsonl(paths["rig_rows"]),
        "runtime_questions": json.loads(paths["runtime_questions"].read_text(encoding="utf-8")),
        "provider_requests": provider_requests,
        "provider_tokens": total_tokens,
        "provider_cost": actual_cost,
        "source_artifacts": digests,
    }


def build_arm_run(combined_receipt_path: Path) -> dict[str, Any]:
    combined_path = combined_receipt_path.resolve()
    combined = _validate_receipt(
        _load_json(combined_path),
        name="combined official receipt",
        domain="combined",
    )
    source_runs = _validate_source_runs(combined.get("source_runs"))
    domains = source_runs["domains"]
    domain_runs = {
        domain: _domain_artifacts(
            domain=domain,
            source=domains[domain],
            combined_receipt_path=combined_path,
        )
        for domain in sorted(rig.DOMAINS)
    }
    _validate_cross_domain_identity(domain_runs)
    first = domain_runs[sorted(rig.DOMAINS)[0]]
    stack = {
        **first["stack_fragment"],
        "dataset_sha256_by_domain": {
            domain: domain_runs[domain]["dataset_digest"] for domain in sorted(rig.DOMAINS)
        },
    }
    validated_stack = rig.validate_stack(stack)
    stack_digest = rig.stack_fingerprint(validated_stack)
    rows: list[dict[str, Any]] = []
    lever_activity: dict[str, int] = {}
    lever_activity_by_domain: dict[str, dict[str, int]] = {}
    for domain in sorted(rig.DOMAINS):
        domain_run = domain_runs[domain]
        domain_rows, domain_levers = _build_rows(
            domain=domain,
            plan=domain_run,
            configuration=domain_run["configuration"],
            stack_digest=stack_digest,
            per_question=domain_run["per_question"],
            rig_rows=domain_run["rig_rows"],
            runtime_questions=domain_run["runtime_questions"],
        )
        rows.extend(domain_rows)
        lever_activity_by_domain[domain] = domain_levers
        for lever, count in domain_levers.items():
            lever_activity[lever] = lever_activity.get(lever, 0) + count
    if set(lever_activity_by_domain["web"]) != set(lever_activity_by_domain["enterprise"]):
        raise BridgeInputError("cross-domain lever activity keys differ")
    role = str(first["arm_role"])
    if role == "render_treatment":
        if not lever_activity:
            raise BridgeInputError("render treatment has no measured lever activity")
    elif lever_activity:
        raise BridgeInputError("baseline or control arm reports treatment lever activity")
    max_spend = sum(float(domain_runs[domain]["max_spend_usd"]) for domain in rig.DOMAINS)
    actual_cost = sum(float(domain_runs[domain]["provider_cost"]) for domain in rig.DOMAINS)
    if actual_cost > max_spend:
        raise BridgeInputError("arm provider cost exceeds its approved two-domain spend")
    payload = {
        "schema_version": rig.ARM_RUN_SCHEMA_VERSION,
        "experiment_id": first["experiment_id"],
        "experiment_phase": first["experiment_phase"],
        "pass_id": first["pass_id"],
        "seed": first["pass_seed"],
        "name": role,
        "substrate": first["substrate"],
        "preregistration_sha256": first["preregistration_sha256"],
        "execution": first["execution"],
        "stack": validated_stack,
        "configuration": first["configuration"],
        "geometry": first["geometry"],
        "rows": rows,
        "provider_usage": {
            "complete": True,
            "requests": sum(
                int(domain_runs[domain]["provider_requests"]) for domain in rig.DOMAINS
            ),
            "total_tokens": sum(
                int(domain_runs[domain]["provider_tokens"]) for domain in rig.DOMAINS
            ),
            "actual_cost_usd": actual_cost,
            "max_spend_usd_total": max_spend,
            "run_ids_by_domain": {
                domain: domain_runs[domain]["provider_usage_run_id"]
                for domain in sorted(rig.DOMAINS)
            },
        },
        "lever_activity": lever_activity,
        "source_artifacts": {
            domain: domain_runs[domain]["source_artifacts"] for domain in sorted(rig.DOMAINS)
        },
        "official_question_count_by_domain": {
            domain: domain_runs[domain]["official_question_count"] for domain in sorted(rig.DOMAINS)
        },
        "official_question_ids_sha256_by_domain": {
            domain: domain_runs[domain]["official_question_ids_sha256"]
            for domain in sorted(rig.DOMAINS)
        },
        "question_order_sha256": rig.canonical_sha256(
            [[row["domain"], row["question_id"]] for row in rows]
        ),
    }
    payload["arm_run_sha256"] = rig.canonical_sha256(payload)
    rig.validate_arm(payload, stack_digest=stack_digest, side=role)
    for domain, artifacts in payload["source_artifacts"].items():
        if set(artifacts) != SOURCE_ARTIFACT_NAMES:
            raise BridgeInputError(f"{domain} source artifact set is not exact")
    return payload


def build_paired_pass(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise BridgeInputError("paired arm runs must be objects")
    left_stack = rig.validate_stack(left.get("stack"))
    right_stack = rig.validate_stack(right.get("stack"))
    if left_stack != right_stack:
        raise BridgeInputError("paired arm stacks differ")
    stack_digest = rig.stack_fingerprint(left_stack)
    validated_left = rig.validate_arm(left, stack_digest=stack_digest, side="left")
    validated_right = rig.validate_arm(right, stack_digest=stack_digest, side="right")
    payload = {
        "schema_version": rig.RUN_PAIR_SCHEMA_VERSION,
        "experiment_id": validated_left["experiment_id"],
        "experiment_phase": validated_left["experiment_phase"],
        "pass_id": validated_left["pass_id"],
        "seed": validated_left["seed"],
        "stack": left_stack,
        "preregistration_sha256": validated_left["preregistration_sha256"],
        "arms": {"left": validated_left, "right": validated_right},
    }
    payload["paired_pass_sha256"] = rig.canonical_sha256(payload)
    rig.validate_pass(payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    arm = subparsers.add_parser("arm")
    arm.add_argument("--official-receipt", required=True)
    arm.add_argument("--output", required=True)
    pair = subparsers.add_parser("pair")
    pair.add_argument("--left-arm", required=True)
    pair.add_argument("--right-arm", required=True)
    pair.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = Path(args.output)
    try:
        if args.command == "arm":
            payload = build_arm_run(Path(args.official_receipt))
        elif args.command == "pair":
            payload = build_paired_pass(
                _load_json(Path(args.left_arm)),
                _load_json(Path(args.right_arm)),
            )
        else:
            raise RuntimeError(f"unknown command {args.command!r}")
    except (OSError, json.JSONDecodeError, rig.RigInputError) as exc:
        payload = {
            "schema_version": rig.FAILURE_SCHEMA_VERSION,
            "status": "FAIL",
            "score_claim_allowed": False,
            "paid_benchmark_allowed": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        rig.write_json(output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))  # noqa: T201
        return 1
    rig.write_json(output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
