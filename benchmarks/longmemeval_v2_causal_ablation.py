"""Plan and execute causal LongMemEval-V2 retrieval ablations."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.longmemeval_v2_reader_holdout import (
    TOKEN_BOUNDED_MAX_RATIO,
    allocate_stratum_quotas,
    derive_holdout_command,
    require_reader_holdout_plan,
    run_score_blind_reader_plan,
)
from benchmarks.longmemeval_v2_reader_holdout_report import (
    HOLDOUT_REPORT_SCHEMA_VERSION,
    build_reader_holdout_report,
)
from benchmarks.longmemeval_v2_reader_replication import (
    DEFAULT_MAX_WORKERS,
    PASS_COUNT,
    PASS_SEEDS,
    pass_id,
    replace_option,
    set_option,
)
from benchmarks.longmemeval_v2_reader_report import (
    DOMAINS,
    expected_run_config,
    load_json,
    sha256_file,
)

CAUSAL_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-causal-ablation-plan-v1"
CAUSAL_REPORT_SCHEMA_VERSION = "sibyl-longmemeval-v2-causal-ablation-report-v1"
CONFIRMATION_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-causal-confirmation-plan-v1"
CONFIRMATION_REPORT_SCHEMA_VERSION = "sibyl-longmemeval-v2-causal-confirmation-report-v1"

BASELINE_CONFIGURATION = "baseline_fixed_reader"
NEIGHBOR_CONFIGURATION = "neighbor_only_bounded_1_2x"
REFINEMENT_CONFIGURATION = "refinement_only_bounded_1_2x"
BUNDLE_CONFIGURATION = "neighbor_refinement_bounded_1_2x"
CAUSAL_CONFIGURATIONS = (
    BASELINE_CONFIGURATION,
    NEIGHBOR_CONFIGURATION,
    REFINEMENT_CONFIGURATION,
    BUNDLE_CONFIGURATION,
)
NEW_DEVELOPMENT_CONFIGURATIONS = (
    NEIGHBOR_CONFIGURATION,
    REFINEMENT_CONFIGURATION,
)
TREATMENT_CONFIGURATIONS = (
    NEIGHBOR_CONFIGURATION,
    REFINEMENT_CONFIGURATION,
    BUNDLE_CONFIGURATION,
)
SOURCE_CONFIGURATION = {
    BASELINE_CONFIGURATION: "baseline_fixed_reader",
    NEIGHBOR_CONFIGURATION: "winner_token_bounded_1_2x",
    REFINEMENT_CONFIGURATION: "winner_token_bounded_1_2x",
    BUNDLE_CONFIGURATION: "winner_token_bounded_1_2x",
}
CONFIGURATION_FACTORS = {
    BASELINE_CONFIGURATION: {"neighbor_stitching": False, "state_part_refinement": False},
    NEIGHBOR_CONFIGURATION: {"neighbor_stitching": True, "state_part_refinement": False},
    REFINEMENT_CONFIGURATION: {"neighbor_stitching": False, "state_part_refinement": True},
    BUNDLE_CONFIGURATION: {"neighbor_stitching": True, "state_part_refinement": True},
}
FACTOR_COMMAND_KEYS = (
    "max_context_items",
    "neighbor_stitch_items",
    "neighbor_stitch_span",
    "state_part_refinement",
    "context_expansion_max_ratio",
)
FACTOR_VALUE_FLAGS = {
    "--max-context-items",
    "--neighbor-stitch-items",
    "--neighbor-stitch-span",
    "--context-expansion-max-ratio",
}
FACTOR_BOOLEAN_FLAGS = {
    "--state-part-refinement",
    "--no-state-part-refinement",
}

DEVELOPMENT_QUESTIONS_PER_DOMAIN = 20
CONFIRMATION_QUESTIONS_PER_DOMAIN = 48
DEVELOPMENT_SAMPLE_SEED = 20_260_714
CONFIRMATION_SAMPLE_SEED = 20_260_715
MAX_DEVELOPMENT_MODEL_COST_USD = 3.0
MAX_CONFIRMATION_MODEL_COST_USD = 8.0
MAX_PILOT_COST_MULTIPLIER = 5.0


def build_causal_ablation_plan(
    *,
    holdout_plan: dict[str, Any],
    holdout_plan_path: Path,
    holdout_report: dict[str, Any],
    holdout_report_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    holdout_plan_path = holdout_plan_path.expanduser().resolve()
    holdout_report_path = holdout_report_path.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    require_source_artifacts(
        holdout_plan=holdout_plan,
        holdout_plan_path=holdout_plan_path,
        holdout_report=holdout_report,
        holdout_report_path=holdout_report_path,
    )
    source_runs = source_run_map(holdout_plan["runs"])
    development_selection = select_development_questions(holdout_plan["selection"])
    questions_path = Path(holdout_plan["source_artifacts"]["questions"]["path"])
    confirmation_selection = select_confirmation_questions(
        questions_path,
        holdout_selection=holdout_plan["selection"],
    )
    existing_runs, development_runs, confirmation_templates = build_causal_run_records(
        source_runs=source_runs,
        development_selection=development_selection,
        confirmation_selection=confirmation_selection,
        output_root=output_root,
    )
    cost_budget = causal_cost_budget(holdout_report)
    plan = {
        "schema_version": CAUSAL_PLAN_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "output_root": str(output_root),
        "configurations": list(CAUSAL_CONFIGURATIONS),
        "configuration_factors": CONFIGURATION_FACTORS,
        "development_selection": development_selection,
        "confirmation_selection": confirmation_selection,
        "protocol": causal_protocol(),
        "analysis": causal_analysis(),
        "cost_budget": cost_budget,
        "integrity_contract": causal_integrity_contract(),
        "existing_runs": existing_runs,
        "runs": development_runs,
        "confirmation_templates": confirmation_templates,
        "source_artifacts": {
            "holdout_plan": {
                "path": str(holdout_plan_path),
                "sha256": sha256_file(holdout_plan_path),
            },
            "holdout_report": {
                "path": str(holdout_report_path),
                "sha256": sha256_file(holdout_report_path),
            },
            "questions": {
                "path": str(questions_path.resolve()),
                "sha256": sha256_file(questions_path),
            },
        },
    }
    require_causal_ablation_plan(plan)
    return plan


def causal_protocol() -> dict[str, Any]:
    return {
        "passes_per_configuration": PASS_COUNT,
        "question_order_seeds": list(PASS_SEEDS),
        "development_questions_per_domain": DEVELOPMENT_QUESTIONS_PER_DOMAIN,
        "development_question_count": DEVELOPMENT_QUESTIONS_PER_DOMAIN * len(DOMAINS),
        "new_development_configurations": list(NEW_DEVELOPMENT_CONFIGURATIONS),
        "existing_development_configurations": [
            BASELINE_CONFIGURATION,
            BUNDLE_CONFIGURATION,
        ],
        "confirmation_questions_per_domain": CONFIRMATION_QUESTIONS_PER_DOMAIN,
        "confirmation_question_count": CONFIRMATION_QUESTIONS_PER_DOMAIN * len(DOMAINS),
        "fixed_sample_no_sequential_stopping": True,
        "all_new_runs_are_fresh": True,
        "execution_wave": "pass",
        "max_workers_cap": DEFAULT_MAX_WORKERS,
        "score_visibility_during_execution": "none",
    }


def causal_analysis() -> dict[str, Any]:
    return {
        "decision_rule_version": "bounded-two-by-two-development-screen-v1",
        "unit": "question",
        "passes_required": PASS_COUNT,
        "minimum_mean_accuracy_delta": 0.02,
        "minimum_probability_positive": 0.65,
        "minimum_majority_vote_delta": 0.0,
        "maximum_memory_token_inflation": 0.25,
        "minimum_domain_delta": -0.05,
        "simplicity_margin": 0.01,
        "selection_tiebreakers": [
            "fewest_active_factors_within_simplicity_margin",
            "lowest_memory_token_inflation",
            "configuration_name",
        ],
        "no_eligible_treatment": "STOP_WITHOUT_CONFIRMATION",
        "confirmation": {
            "minimum_mean_accuracy_delta": 0.03,
            "minimum_probability_positive": 0.85,
            "minimum_confidence_interval_lower": -0.02,
            "maximum_memory_token_inflation": 0.25,
            "minimum_domain_delta": -0.02,
            "no_go_max_probability_positive": 0.5,
            "otherwise": "RESEARCH-MORE",
        },
    }


def causal_integrity_contract() -> dict[str, Any]:
    return {
        "development_data_status": "consumed_holdout; development_only",
        "confirmation_data_status": "fresh_and_score_blind",
        "sampling_inputs": ["question_id", "domain", "question_type"],
        "sampling_uses_question_text": False,
        "sampling_uses_answers_or_scores": False,
        "confirmation_selection_precedes_development_scores": True,
        "candidate_selection_rule_precedes_development_scores": True,
        "interim_score_inspection_allowed": False,
        "all_passes_required_for_selection_and_confirmation": True,
        "memory_builds_reused": True,
        "question_ids_visible_to_memory": False,
        "claim_level": "question-level confirmation on shared small-tier domain haystacks",
    }


def confirmation_protocol() -> dict[str, Any]:
    return {
        "passes_per_configuration": PASS_COUNT,
        "question_order_seeds": list(PASS_SEEDS),
        "question_count": CONFIRMATION_QUESTIONS_PER_DOMAIN * len(DOMAINS),
        "questions_per_domain": CONFIRMATION_QUESTIONS_PER_DOMAIN,
        "fixed_sample_no_sequential_stopping": True,
        "all_runs_are_fresh": True,
        "execution_wave": "pass",
        "max_workers_cap": DEFAULT_MAX_WORKERS,
        "score_visibility_during_execution": "none",
    }


def confirmation_cost_budget(causal_plan: dict[str, Any]) -> dict[str, Any]:
    cost_budget = causal_plan["cost_budget"]
    return {
        "estimated_total_model_cost_usd": cost_budget["estimated_confirmation_model_cost_usd"],
        "max_total_model_cost_usd": cost_budget["max_confirmation_model_cost_usd"],
        "pilot_cost_per_question_run_usd": cost_budget["pilot_cost_per_question_run_usd"],
        "max_pilot_cost_multiplier": cost_budget["max_pilot_cost_multiplier"],
    }


def confirmation_integrity_contract() -> dict[str, Any]:
    return {
        "candidate_selected_by_frozen_development_rule": True,
        "confirmation_questions_were_frozen_before_development_scores": True,
        "interim_score_inspection_allowed": False,
        "all_passes_required_for_decision": True,
        "memory_builds_reused": True,
        "claim_level": "question-level holdout on shared small-tier domain haystacks",
    }


def select_development_questions(holdout_selection: dict[str, Any]) -> dict[str, Any]:
    ids_by_domain = holdout_selection.get("question_ids_by_domain")
    types_by_domain = holdout_selection.get("question_types_by_domain")
    if not isinstance(ids_by_domain, dict) or not isinstance(types_by_domain, dict):
        raise TypeError("Holdout selection is incomplete")
    pool = {
        domain: {
            str(question_id): str(types_by_domain[domain][question_id])
            for question_id in ids_by_domain[domain]
        }
        for domain in DOMAINS
    }
    return select_stratified_questions(
        pool,
        questions_per_domain=DEVELOPMENT_QUESTIONS_PER_DOMAIN,
        seed=DEVELOPMENT_SAMPLE_SEED,
        source="consumed_holdout",
    )


def select_confirmation_questions(
    questions_path: Path,
    *,
    holdout_selection: dict[str, Any],
) -> dict[str, Any]:
    excluded = {
        domain: set(holdout_selection["excluded_question_ids_by_domain"][domain])
        | set(holdout_selection["question_ids_by_domain"][domain])
        for domain in DOMAINS
    }
    pool: dict[str, dict[str, str]] = {domain: {} for domain in DOMAINS}
    observed = set()
    with questions_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise TypeError(f"Expected JSON object at {questions_path}:{line_number}")
            question_id = str(raw.get("id") or "")
            domain = str(raw.get("domain") or "")
            question_type = str(raw.get("question_type") or "")
            if not question_id or question_id in observed:
                raise ValueError(f"Question metadata has an invalid id: {question_id!r}")
            observed.add(question_id)
            if domain in DOMAINS and question_id not in excluded[domain]:
                if not question_type:
                    raise ValueError(f"Question {question_id!r} has no question_type")
                pool[domain][question_id] = question_type
    if any(not ids <= observed for ids in excluded.values()):
        raise ValueError("Excluded causal-ablation questions are missing")
    return select_stratified_questions(
        pool,
        questions_per_domain=CONFIRMATION_QUESTIONS_PER_DOMAIN,
        seed=CONFIRMATION_SAMPLE_SEED,
        source="unseen_question_pool",
        excluded={domain: sorted(ids) for domain, ids in excluded.items()},
    )


def select_stratified_questions(
    pool: dict[str, dict[str, str]],
    *,
    questions_per_domain: int,
    seed: int,
    source: str,
    excluded: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    selected_ids = {}
    selected_types = {}
    available_counts = {}
    selected_counts = {}
    for domain in DOMAINS:
        by_type: dict[str, list[str]] = defaultdict(list)
        for question_id, question_type in pool[domain].items():
            by_type[question_type].append(question_id)
        quotas = allocate_stratum_quotas(
            {question_type: len(ids) for question_type, ids in by_type.items()},
            sample_size=questions_per_domain,
        )
        chosen = []
        types = {}
        for question_type, quota in sorted(quotas.items()):
            ranked = sorted(
                by_type[question_type],
                key=lambda question_id: sample_key(
                    seed=seed,
                    domain=domain,
                    question_type=question_type,
                    question_id=question_id,
                ),
            )
            for question_id in ranked[:quota]:
                chosen.append(question_id)
                types[question_id] = question_type
        selected_ids[domain] = sorted(chosen)
        selected_types[domain] = {question_id: types[question_id] for question_id in sorted(types)}
        available_counts[domain] = {
            question_type: len(ids) for question_type, ids in sorted(by_type.items())
        }
        selected_counts[domain] = dict(sorted(quotas.items()))
    return {
        "algorithm": "minimum-one stratified largest-remainder allocation with SHA-256 ranking",
        "seed": seed,
        "source": source,
        "questions_per_domain": questions_per_domain,
        "available_question_type_counts_by_domain": available_counts,
        "selected_question_type_counts_by_domain": selected_counts,
        "question_ids_by_domain": selected_ids,
        "question_types_by_domain": selected_types,
        **({"excluded_question_ids_by_domain": excluded} if excluded is not None else {}),
    }


def sample_key(
    *,
    seed: int,
    domain: str,
    question_type: str,
    question_id: str,
) -> str:
    value = f"{seed}:{domain}:{question_type}:{question_id}"
    return hashlib.sha256(value.encode()).hexdigest()


def source_run_map(runs: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    result = {}
    for run in runs:
        key = (str(run["pass_id"]), str(run["configuration"]), str(run["domain"]))
        if key in result:
            raise ValueError(f"Duplicate source run: {key}")
        result[key] = run
    return result


def build_causal_run_records(
    *,
    source_runs: dict[tuple[str, str, str], dict[str, Any]],
    development_selection: dict[str, Any],
    confirmation_selection: dict[str, Any],
    output_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    require_factorial_source_isolation(source_runs)
    existing = []
    development = []
    confirmation = []
    for pass_index, shuffle_seed in enumerate(PASS_SEEDS, start=1):
        pass_name = pass_id(pass_index)
        for configuration in CAUSAL_CONFIGURATIONS:
            source_configuration = SOURCE_CONFIGURATION[configuration]
            for domain in DOMAINS:
                source = source_runs[(pass_name, source_configuration, domain)]
                if configuration in {BASELINE_CONFIGURATION, BUNDLE_CONFIGURATION}:
                    existing.append(
                        {
                            "pass_id": pass_name,
                            "pass_index": pass_index,
                            "configuration": configuration,
                            "source_configuration": source_configuration,
                            "domain": domain,
                            "selected_question_ids": development_selection[
                                "question_ids_by_domain"
                            ][domain],
                            "output_dir": source["output_dir"],
                            "command": source["command"],
                        }
                    )
                else:
                    output_dir = (
                        output_root / "development" / "runs" / pass_name / configuration / domain
                    )
                    development.append(
                        run_record(
                            source=source["command"],
                            configuration=configuration,
                            domain=domain,
                            pass_index=pass_index,
                            shuffle_seed=shuffle_seed,
                            question_ids=development_selection["question_ids_by_domain"][domain],
                            output_dir=output_dir,
                        )
                    )
                confirmation_dir = (
                    output_root / "confirmation" / "runs" / pass_name / configuration / domain
                )
                confirmation.append(
                    run_record(
                        source=source["command"],
                        configuration=configuration,
                        domain=domain,
                        pass_index=pass_index,
                        shuffle_seed=shuffle_seed,
                        question_ids=confirmation_selection["question_ids_by_domain"][domain],
                        output_dir=confirmation_dir,
                    )
                )
    return existing, development, confirmation


def require_factorial_source_isolation(
    source_runs: dict[tuple[str, str, str], dict[str, Any]],
) -> None:
    for pass_index in range(1, PASS_COUNT + 1):
        pass_name = pass_id(pass_index)
        for domain in DOMAINS:
            parsed = {}
            for configuration in (BASELINE_CONFIGURATION, BUNDLE_CONFIGURATION):
                source = source_runs[(pass_name, SOURCE_CONFIGURATION[configuration], domain)]
                actual = expected_run_config(source["command"])
                configured = expected_run_config(
                    configure_command(source["command"], configuration=configuration)
                )
                if any(actual[key] != configured[key] for key in FACTOR_COMMAND_KEYS):
                    raise ValueError(
                        f"Source run does not match {configuration} factors: {pass_name}/{domain}"
                    )
                parsed[configuration] = actual
            shared = {
                configuration: {
                    key: value
                    for key, value in run.items()
                    if key not in {*FACTOR_COMMAND_KEYS, "output_dir"}
                }
                for configuration, run in parsed.items()
            }
            if shared[BASELINE_CONFIGURATION] != shared[BUNDLE_CONFIGURATION]:
                raise ValueError(f"Causal source runs differ outside factors: {pass_name}/{domain}")
            raw = {
                configuration: source_command_without_factor_overrides(
                    source_runs[(pass_name, SOURCE_CONFIGURATION[configuration], domain)]["command"]
                )
                for configuration in (BASELINE_CONFIGURATION, BUNDLE_CONFIGURATION)
            }
            if raw[BASELINE_CONFIGURATION] != raw[BUNDLE_CONFIGURATION]:
                raise ValueError(
                    f"Causal source raw commands differ outside factors: {pass_name}/{domain}"
                )


def source_command_without_factor_overrides(command: list[str]) -> list[str]:
    result = []
    skip_value = False
    for value in command:
        if skip_value:
            skip_value = False
        elif value == "--output-dir" or value in FACTOR_VALUE_FLAGS:
            skip_value = True
        elif value not in FACTOR_BOOLEAN_FLAGS:
            result.append(value)
    if skip_value:
        raise ValueError("Causal source command ends with an option missing its value")
    return result


def run_record(
    *,
    source: list[str],
    configuration: str,
    domain: str,
    pass_index: int,
    shuffle_seed: int | None,
    question_ids: list[str],
    output_dir: Path,
) -> dict[str, Any]:
    command = configure_command(source, configuration=configuration)
    command = derive_holdout_command(
        command,
        output_dir=output_dir,
        question_ids=question_ids,
        shuffle_seed=shuffle_seed,
        context_expansion_max_ratio=(
            0.0 if configuration == BASELINE_CONFIGURATION else TOKEN_BOUNDED_MAX_RATIO
        ),
    )
    return {
        "pass_id": pass_id(pass_index),
        "pass_index": pass_index,
        "configuration": configuration,
        "domain": domain,
        "shuffle_questions_seed": shuffle_seed,
        "output_dir": str(output_dir),
        "command": command,
    }


def configure_command(source: list[str], *, configuration: str) -> list[str]:
    factors = CONFIGURATION_FACTORS[configuration]
    result = replace_option(
        source,
        "--max-context-items",
        "10" if factors["neighbor_stitching"] else "8",
    )
    result = replace_option(
        result,
        "--neighbor-stitch-items",
        "2" if factors["neighbor_stitching"] else "0",
    )
    result = replace_option(
        result,
        "--neighbor-stitch-span",
        "1" if factors["neighbor_stitching"] else "0",
    )
    result = set_boolean_option(
        result,
        "--state-part-refinement",
        enabled=factors["state_part_refinement"],
    )
    return set_option(
        result,
        "--context-expansion-max-ratio",
        str(0.0 if configuration == BASELINE_CONFIGURATION else TOKEN_BOUNDED_MAX_RATIO),
    )


def set_boolean_option(command: list[str], flag: str, *, enabled: bool) -> list[str]:
    negative = f"--no-{flag.removeprefix('--')}"
    result = [value for value in command if value not in {flag, negative}]
    result.append(flag if enabled else negative)
    return result


def causal_cost_budget(holdout_report: dict[str, Any]) -> dict[str, Any]:
    total_cost = float(holdout_report["costs"]["total_model_cost_usd"])
    protocol = holdout_report["protocol"]
    holdout_question_runs = (
        int(protocol["question_count"])
        * len(holdout_report["configurations"])
        * int(protocol["passes_per_configuration"])
    )
    cost_per_question_run = total_cost / holdout_question_runs
    development_question_runs = (
        DEVELOPMENT_QUESTIONS_PER_DOMAIN
        * len(DOMAINS)
        * len(NEW_DEVELOPMENT_CONFIGURATIONS)
        * PASS_COUNT
    )
    confirmation_question_runs = CONFIRMATION_QUESTIONS_PER_DOMAIN * len(DOMAINS) * 2 * PASS_COUNT
    development_estimate = cost_per_question_run * development_question_runs
    confirmation_estimate = cost_per_question_run * confirmation_question_runs
    if development_estimate > MAX_DEVELOPMENT_MODEL_COST_USD:
        raise ValueError("Causal development estimate exceeds its model cost budget")
    if confirmation_estimate > MAX_CONFIRMATION_MODEL_COST_USD:
        raise ValueError("Causal confirmation estimate exceeds its model cost budget")
    return {
        "source_model_cost_usd": total_cost,
        "source_question_runs": holdout_question_runs,
        "pilot_cost_per_question_run_usd": cost_per_question_run,
        "development_question_runs": development_question_runs,
        "estimated_development_model_cost_usd": development_estimate,
        "max_development_model_cost_usd": MAX_DEVELOPMENT_MODEL_COST_USD,
        "confirmation_question_runs": confirmation_question_runs,
        "estimated_confirmation_model_cost_usd": confirmation_estimate,
        "max_confirmation_model_cost_usd": MAX_CONFIRMATION_MODEL_COST_USD,
        "max_pilot_cost_multiplier": MAX_PILOT_COST_MULTIPLIER,
        "basis": "provider-reported cost from the completed fresh reader holdout",
    }


def require_source_artifacts(
    *,
    holdout_plan: dict[str, Any],
    holdout_plan_path: Path,
    holdout_report: dict[str, Any],
    holdout_report_path: Path,
) -> None:
    if load_json(holdout_plan_path) != holdout_plan:
        raise ValueError("Holdout plan path does not match the loaded plan")
    if load_json(holdout_report_path) != holdout_report:
        raise ValueError("Holdout report path does not match the loaded report")
    require_reader_holdout_plan(holdout_plan)
    if (
        holdout_report.get("schema_version") != HOLDOUT_REPORT_SCHEMA_VERSION
        or holdout_report.get("status") != "PASS"
        or holdout_report.get("decision", {}).get("valid_experiment") is not True
    ):
        raise ValueError("Causal ablation requires a valid completed holdout report")
    source = holdout_report.get("source_artifacts", {}).get("holdout_plan")
    if (
        not isinstance(source, dict)
        or Path(str(source.get("path") or "")).resolve() != holdout_plan_path
        or source.get("sha256") != sha256_file(holdout_plan_path)
    ):
        raise ValueError("Holdout report changed its plan binding")
    recomputed = build_reader_holdout_report(plan=holdout_plan, plan_path=holdout_plan_path)
    if not reports_match_except_created_at(holdout_report, recomputed):
        raise ValueError("Holdout report changed after scoring")


def reports_match_except_created_at(
    actual: dict[str, Any],
    recomputed: dict[str, Any],
) -> bool:
    comparable_keys = set(recomputed) - {"created_at"}
    return set(actual) == set(recomputed) and all(
        actual.get(key) == recomputed[key] for key in comparable_keys
    )


def require_causal_ablation_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    require_causal_static_contract(plan)
    sources = require_causal_source_bindings(plan)
    holdout_plan_path = Path(sources["holdout_plan"]["path"])
    holdout_report_path = Path(sources["holdout_report"]["path"])
    holdout_plan = load_json(holdout_plan_path)
    holdout_report = load_json(holdout_report_path)
    require_source_artifacts(
        holdout_plan=holdout_plan,
        holdout_plan_path=holdout_plan_path,
        holdout_report=holdout_report,
        holdout_report_path=holdout_report_path,
    )
    expected_development = select_development_questions(holdout_plan["selection"])
    expected_confirmation = select_confirmation_questions(
        Path(sources["questions"]["path"]),
        holdout_selection=holdout_plan["selection"],
    )
    if plan.get("development_selection") != expected_development:
        raise ValueError("Causal development selection changed")
    if plan.get("confirmation_selection") != expected_confirmation:
        raise ValueError("Causal confirmation selection changed")
    if plan.get("cost_budget") != causal_cost_budget(holdout_report):
        raise ValueError("Causal ablation cost budget changed")
    expected_existing, expected_runs, expected_templates = build_causal_run_records(
        source_runs=source_run_map(holdout_plan["runs"]),
        development_selection=expected_development,
        confirmation_selection=expected_confirmation,
        output_root=Path(plan["output_root"]).resolve(),
    )
    if plan.get("existing_runs") != expected_existing:
        raise ValueError("Causal existing run bindings changed")
    if plan.get("runs") != expected_runs:
        raise ValueError("Causal development commands changed")
    if plan.get("confirmation_templates") != expected_templates:
        raise ValueError("Causal confirmation templates changed")
    return expected_runs


def require_causal_static_contract(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != CAUSAL_PLAN_SCHEMA_VERSION:
        raise ValueError("Invalid causal ablation plan")
    if plan.get("configurations") != list(CAUSAL_CONFIGURATIONS):
        raise ValueError("Causal ablation plan changed its configurations")
    if plan.get("configuration_factors") != CONFIGURATION_FACTORS:
        raise ValueError("Causal ablation plan changed its factors")
    if plan.get("protocol") != causal_protocol():
        raise ValueError("Causal ablation plan changed its protocol")
    if plan.get("analysis") != causal_analysis():
        raise ValueError("Causal ablation plan changed its analysis")
    if plan.get("integrity_contract") != causal_integrity_contract():
        raise ValueError("Causal ablation plan changed its integrity contract")


def require_causal_source_bindings(plan: dict[str, Any]) -> dict[str, Any]:
    sources = plan.get("source_artifacts")
    if not isinstance(sources, dict):
        raise TypeError("Causal ablation plan has no source artifacts")
    for name in ("holdout_plan", "holdout_report", "questions"):
        source = sources.get(name)
        if not isinstance(source, dict):
            raise TypeError(f"Causal ablation plan has no {name} binding")
        path = Path(str(source.get("path") or "")).resolve()
        if not path.is_file() or source.get("sha256") != sha256_file(path):
            raise ValueError(f"Causal ablation {name} source changed")
    return sources


def run_causal_development_plan(
    plan: dict[str, Any],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    runs = require_causal_ablation_plan(plan)
    runner_plan = {
        "protocol": plan["protocol"],
        "cost_budget": {
            "max_total_model_cost_usd": plan["cost_budget"]["max_development_model_cost_usd"],
            "pilot_cost_per_question_run_usd": plan["cost_budget"][
                "pilot_cost_per_question_run_usd"
            ],
            "max_pilot_cost_multiplier": plan["cost_budget"]["max_pilot_cost_multiplier"],
        },
    }
    return run_score_blind_reader_plan(
        runner_plan,
        runs=runs,
        pass_count=PASS_COUNT,
        max_workers=max_workers,
    )


def build_causal_confirmation_plan(
    *,
    causal_plan: dict[str, Any],
    causal_plan_path: Path,
    causal_report: dict[str, Any],
    causal_report_path: Path,
) -> dict[str, Any]:
    causal_plan_path = causal_plan_path.expanduser().resolve()
    causal_report_path = causal_report_path.expanduser().resolve()
    if load_json(causal_plan_path) != causal_plan:
        raise ValueError("Causal plan path does not match the loaded plan")
    if load_json(causal_report_path) != causal_report:
        raise ValueError("Causal report path does not match the loaded report")
    require_causal_ablation_plan(causal_plan)
    require_causal_report_binding(
        causal_report,
        causal_plan_path=causal_plan_path,
    )
    selected = causal_report.get("decision", {}).get("selected_configuration")
    if selected not in TREATMENT_CONFIGURATIONS:
        raise ValueError("Causal development report selected no confirmation treatment")
    runs = [
        run
        for run in causal_plan["confirmation_templates"]
        if run["configuration"] in {BASELINE_CONFIGURATION, selected}
    ]
    plan = {
        "schema_version": CONFIRMATION_PLAN_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "output_root": str(Path(causal_plan["output_root"]) / "confirmation"),
        "configurations": [BASELINE_CONFIGURATION, selected],
        "selected_configuration": selected,
        "selection": causal_plan["confirmation_selection"],
        "protocol": confirmation_protocol(),
        "analysis": causal_plan["analysis"]["confirmation"],
        "cost_budget": confirmation_cost_budget(causal_plan),
        "integrity_contract": confirmation_integrity_contract(),
        "runs": runs,
        "source_artifacts": {
            "causal_plan": {
                "path": str(causal_plan_path),
                "sha256": sha256_file(causal_plan_path),
            },
            "causal_report": {
                "path": str(causal_report_path),
                "sha256": sha256_file(causal_report_path),
            },
        },
    }
    require_causal_confirmation_plan(plan)
    return plan


def require_causal_report_binding(
    report: dict[str, Any],
    *,
    causal_plan_path: Path,
) -> None:
    source = report.get("source_artifacts", {}).get("causal_plan")
    if (
        report.get("schema_version") != CAUSAL_REPORT_SCHEMA_VERSION
        or report.get("status") != "PASS"
        or not isinstance(source, dict)
        or Path(str(source.get("path") or "")).resolve() != causal_plan_path
        or source.get("sha256") != sha256_file(causal_plan_path)
    ):
        raise ValueError("Invalid causal development report binding")
    causal_plan = load_json(causal_plan_path)
    expected = rebuild_causal_ablation_report(
        causal_plan=causal_plan,
        causal_plan_path=causal_plan_path,
    )
    if not reports_match_except_created_at(report, expected):
        raise ValueError("Causal development report changed after scoring")


def rebuild_causal_ablation_report(
    *,
    causal_plan: dict[str, Any],
    causal_plan_path: Path,
) -> dict[str, Any]:
    from benchmarks.longmemeval_v2_causal_ablation_report import (  # noqa: PLC0415
        build_causal_ablation_report,
    )

    return build_causal_ablation_report(
        plan=causal_plan,
        plan_path=causal_plan_path,
    )


def require_causal_confirmation_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if plan.get("schema_version") != CONFIRMATION_PLAN_SCHEMA_VERSION:
        raise ValueError("Invalid causal confirmation plan")
    sources = plan.get("source_artifacts")
    if not isinstance(sources, dict):
        raise TypeError("Causal confirmation plan has no source artifacts")
    for name in ("causal_plan", "causal_report"):
        source = sources.get(name)
        if not isinstance(source, dict):
            raise TypeError(f"Causal confirmation plan has no {name} binding")
        path = Path(str(source.get("path") or "")).resolve()
        if not path.is_file() or source.get("sha256") != sha256_file(path):
            raise ValueError(f"Causal confirmation {name} source changed")
    causal_plan_path = Path(sources["causal_plan"]["path"])
    causal_report_path = Path(sources["causal_report"]["path"])
    causal_plan = load_json(causal_plan_path)
    causal_report = load_json(causal_report_path)
    require_causal_ablation_plan(causal_plan)
    require_causal_report_binding(causal_report, causal_plan_path=causal_plan_path)
    selected = causal_report.get("decision", {}).get("selected_configuration")
    if selected not in TREATMENT_CONFIGURATIONS:
        raise ValueError("Causal confirmation source selected no treatment")
    expected_runs = [
        run
        for run in causal_plan["confirmation_templates"]
        if run["configuration"] in {BASELINE_CONFIGURATION, selected}
    ]
    require_causal_confirmation_static_contract(
        plan,
        causal_plan=causal_plan,
        selected=selected,
    )
    if plan.get("runs") != expected_runs:
        raise ValueError("Causal confirmation commands changed")
    return expected_runs


def require_causal_confirmation_static_contract(
    plan: dict[str, Any],
    *,
    causal_plan: dict[str, Any],
    selected: str,
) -> None:
    if plan.get("configurations") != [BASELINE_CONFIGURATION, selected]:
        raise ValueError("Causal confirmation configurations changed")
    if plan.get("selected_configuration") != selected:
        raise ValueError("Causal confirmation selection changed")
    if plan.get("selection") != causal_plan["confirmation_selection"]:
        raise ValueError("Causal confirmation questions changed")
    expected_output_root = Path(causal_plan["output_root"]) / "confirmation"
    if Path(str(plan.get("output_root") or "")).resolve() != expected_output_root.resolve():
        raise ValueError("Causal confirmation output root changed")
    if plan.get("protocol") != confirmation_protocol():
        raise ValueError("Causal confirmation protocol changed")
    if plan.get("analysis") != causal_plan["analysis"]["confirmation"]:
        raise ValueError("Causal confirmation analysis changed")
    if plan.get("cost_budget") != confirmation_cost_budget(causal_plan):
        raise ValueError("Causal confirmation cost budget changed")
    if plan.get("integrity_contract") != confirmation_integrity_contract():
        raise ValueError("Causal confirmation integrity contract changed")


def run_causal_confirmation_plan(
    plan: dict[str, Any],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    runs = require_causal_confirmation_plan(plan)
    return run_score_blind_reader_plan(
        plan,
        runs=runs,
        pass_count=PASS_COUNT,
        max_workers=max_workers,
    )
