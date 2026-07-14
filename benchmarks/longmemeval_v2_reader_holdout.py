"""Plan and execute the fresh LongMemEval-V2 reader holdout."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.longmemeval_v2_reader_replication import (
    COMMAND_PREFIX,
    DEFAULT_MAX_WORKERS,
    PASS_COUNT,
    PASS_SEEDS,
    PRIMARY_CONFIGURATIONS,
    pass_id,
    replace_option,
    require_reader_replication_plan,
    set_option,
)
from benchmarks.longmemeval_v2_reader_replication_report import (
    REPLICATION_REPORT_SCHEMA_VERSION,
)
from benchmarks.longmemeval_v2_reader_report import (
    DOMAINS,
    expected_run_config,
    load_json,
    reader_model_cost,
    require_reader_run_files,
    sha256_file,
    sha256_question_ids,
    validate_reader_run_identity,
)

ROOT = Path(__file__).resolve().parents[1]
HOLDOUT_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-holdout-plan-v1"
HOLDOUT_CONFIGURATIONS = (
    "baseline_fixed_reader",
    "winner_fixed_reader",
    "winner_token_bounded_1_2x",
)
SOURCE_CONFIGURATION = {
    "baseline_fixed_reader": "baseline_fixed_reader",
    "winner_fixed_reader": "winner_fixed_reader",
    "winner_token_bounded_1_2x": "winner_fixed_reader",
}
QUESTIONS_PER_DOMAIN = 48
TOTAL_QUESTION_COUNT = QUESTIONS_PER_DOMAIN * len(DOMAINS)
HOLDOUT_SAMPLE_SEED = 20_260_713
TOKEN_BOUNDED_MAX_RATIO = 1.2
MAX_TOTAL_MODEL_COST_USD = 8.0
MAX_PILOT_COST_MULTIPLIER = 5.0
MEMORY_ARTIFACTS = (
    "memory_config.json",
    "chunk_catalog.jsonl.gz",
    "memory_manifest.json",
)


def build_reader_holdout_plan(
    *,
    replication_plan: dict[str, Any],
    replication_plan_path: Path,
    replication_report: dict[str, Any],
    replication_report_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    replication_plan_path = replication_plan_path.expanduser().resolve()
    replication_report_path = replication_report_path.expanduser().resolve()
    if load_json(replication_plan_path) != replication_plan:
        raise ValueError("Replication plan path does not match the loaded plan")
    if load_json(replication_report_path) != replication_report:
        raise ValueError("Replication report path does not match the loaded report")
    replication_runs = require_reader_replication_plan(replication_plan)
    validate_replication_report_source(
        replication_report,
        replication_plan_path=replication_plan_path,
    )

    source_commands = source_command_map(replication_runs)
    excluded_ids = excluded_question_ids(source_commands)
    data_root = shared_data_root(source_commands)
    questions_path = data_root / "questions.jsonl"
    selection = select_holdout_questions(questions_path, excluded_ids=excluded_ids)
    cost_budget = holdout_cost_budget(replication_report)
    output_root = output_root.expanduser().resolve()

    runs = []
    for pass_index, shuffle_seed in enumerate(PASS_SEEDS, start=1):
        for configuration in HOLDOUT_CONFIGURATIONS:
            source_configuration = SOURCE_CONFIGURATION[configuration]
            for domain in DOMAINS:
                output_dir = output_root / "runs" / pass_id(pass_index) / configuration / domain
                command = derive_holdout_command(
                    source_commands[(source_configuration, domain)],
                    output_dir=output_dir,
                    question_ids=selection["question_ids_by_domain"][domain],
                    shuffle_seed=shuffle_seed,
                    context_expansion_max_ratio=(
                        TOKEN_BOUNDED_MAX_RATIO
                        if configuration == "winner_token_bounded_1_2x"
                        else 0.0
                    ),
                )
                runs.append(
                    {
                        "pass_id": pass_id(pass_index),
                        "pass_index": pass_index,
                        "configuration": configuration,
                        "domain": domain,
                        "shuffle_questions_seed": shuffle_seed,
                        "output_dir": str(output_dir),
                        "command": command,
                    }
                )

    source_memory = source_memory_artifacts(source_commands)
    plan = {
        "schema_version": HOLDOUT_PLAN_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "output_root": str(output_root),
        "configurations": list(HOLDOUT_CONFIGURATIONS),
        "selection": selection,
        "protocol": {
            "passes_per_configuration": PASS_COUNT,
            "question_order_seeds": list(PASS_SEEDS),
            "question_count": TOTAL_QUESTION_COUNT,
            "questions_per_domain": QUESTIONS_PER_DOMAIN,
            "fixed_sample_no_sequential_stopping": True,
            "all_runs_are_fresh": True,
            "execution_wave": "pass",
            "max_workers_cap": DEFAULT_MAX_WORKERS,
            "score_visibility_during_execution": "none",
        },
        "analysis": {
            "unit": "question",
            "passes_required": PASS_COUNT,
            "primary_contrast": "winner_token_bounded_1_2x_vs_baseline_fixed_reader",
            "secondary_contrasts": [
                "winner_fixed_reader_vs_baseline_fixed_reader",
                "winner_fixed_reader_vs_winner_token_bounded_1_2x",
            ],
            "go_min_mean_accuracy_delta": 0.03,
            "go_min_probability_positive": 0.85,
            "go_min_confidence_interval_lower": -0.02,
            "go_max_memory_token_inflation": 0.25,
            "candidate_selection_min_delta_over_bounded": 0.03,
            "minimum_bounded_binding_rate": 0.15,
            "no_go_max_probability_positive": 0.5,
            "otherwise": "RESEARCH-MORE",
        },
        "cost_budget": cost_budget,
        "integrity_contract": {
            "sampling_inputs": ["question_id", "domain", "question_type"],
            "sampling_uses_question_text": False,
            "sampling_uses_answers_or_scores": False,
            "configuration_selection_uses_holdout_scores": False,
            "all_passes_required_for_decision": True,
            "interim_score_inspection_allowed": False,
            "memory_builds_reused": True,
            "claim_level": "question-level holdout on shared small-tier domain haystacks",
        },
        "runs": runs,
        "source_artifacts": {
            "replication_plan": {
                "path": str(replication_plan_path),
                "sha256": sha256_file(replication_plan_path),
            },
            "replication_report": {
                "path": str(replication_report_path),
                "sha256": sha256_file(replication_report_path),
            },
            "questions": {
                "path": str(questions_path.resolve()),
                "sha256": sha256_file(questions_path),
            },
            "memory": source_memory,
        },
    }
    require_reader_holdout_plan(plan)
    return plan


def validate_replication_report_source(
    report: dict[str, Any],
    *,
    replication_plan_path: Path,
) -> None:
    if report.get("schema_version") != REPLICATION_REPORT_SCHEMA_VERSION:
        raise ValueError("Invalid reader replication report")
    if report.get("status") != "PASS":
        raise ValueError("Reader replication report did not pass")
    source = report.get("source_artifacts", {}).get("replication_plan", {})
    if (
        not isinstance(source, dict)
        or Path(str(source.get("path") or "")).resolve() != replication_plan_path
        or source.get("sha256") != sha256_file(replication_plan_path)
    ):
        raise ValueError("Reader replication report changed its plan binding")


def source_command_map(
    runs: list[dict[str, Any]],
) -> dict[tuple[str, str], list[str]]:
    result = {}
    for run in runs:
        if run["pass_index"] != 1 or run["configuration"] not in PRIMARY_CONFIGURATIONS:
            continue
        key = (str(run["configuration"]), str(run["domain"]))
        result[key] = [str(value) for value in run["command"]]
    expected = {
        (configuration, domain) for configuration in PRIMARY_CONFIGURATIONS for domain in DOMAINS
    }
    if set(result) != expected:
        raise ValueError("Replication plan does not expose every holdout source command")
    return result


def excluded_question_ids(
    commands: dict[tuple[str, str], list[str]],
) -> dict[str, list[str]]:
    result = {}
    for domain in DOMAINS:
        baseline = expected_run_config(commands[(PRIMARY_CONFIGURATIONS[0], domain)])[
            "question_ids"
        ]
        candidate = expected_run_config(commands[(PRIMARY_CONFIGURATIONS[1], domain)])[
            "question_ids"
        ]
        if baseline != candidate:
            raise ValueError("Replication configurations differ in diagnostic question coverage")
        result[domain] = sorted(baseline)
    return result


def shared_data_root(commands: dict[tuple[str, str], list[str]]) -> Path:
    roots = {expected_run_config(command)["data_root"] for command in commands.values()}
    if len(roots) != 1 or None in roots:
        raise ValueError("Replication source commands do not share one data root")
    return Path(str(next(iter(roots)))).expanduser().resolve()


def select_holdout_questions(
    questions_path: Path,
    *,
    excluded_ids: dict[str, list[str]],
) -> dict[str, Any]:
    excluded = {domain: set(excluded_ids[domain]) for domain in DOMAINS}
    available: dict[str, dict[str, list[str]]] = {domain: defaultdict(list) for domain in DOMAINS}
    observed_ids = set()
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
            if not question_id or question_id in observed_ids:
                raise ValueError(
                    f"Question metadata has an invalid or duplicate id: {question_id!r}"
                )
            observed_ids.add(question_id)
            if domain not in DOMAINS:
                continue
            if not question_type:
                raise ValueError(f"Question {question_id!r} has no question_type")
            if question_id not in excluded[domain]:
                available[domain][question_type].append(question_id)
    missing_exclusions = {domain: sorted(excluded[domain] - observed_ids) for domain in DOMAINS}
    if any(missing_exclusions.values()):
        raise ValueError(f"Excluded diagnostic questions are missing: {missing_exclusions}")

    selected_ids = {}
    selected_types = {}
    available_counts = {}
    selected_counts = {}
    for domain in DOMAINS:
        quotas = allocate_stratum_quotas(
            {question_type: len(ids) for question_type, ids in available[domain].items()},
            sample_size=QUESTIONS_PER_DOMAIN,
        )
        chosen = []
        types = {}
        for question_type, quota in sorted(quotas.items()):
            ranked = sorted(
                available[domain][question_type],
                key=lambda question_id: stable_sample_key(
                    domain=domain,
                    question_type=question_type,
                    question_id=question_id,
                ),
            )
            for question_id in ranked[:quota]:
                chosen.append(question_id)
                types[question_id] = question_type
        if len(chosen) != QUESTIONS_PER_DOMAIN:
            raise ValueError(f"Holdout selection has the wrong size for {domain}")
        selected_ids[domain] = sorted(chosen)
        selected_types[domain] = {question_id: types[question_id] for question_id in sorted(types)}
        available_counts[domain] = {
            question_type: len(ids) for question_type, ids in sorted(available[domain].items())
        }
        selected_counts[domain] = dict(sorted(quotas.items()))
    return {
        "algorithm": "minimum-one stratified largest-remainder allocation with SHA-256 ranking",
        "seed": HOLDOUT_SAMPLE_SEED,
        "questions_per_domain": QUESTIONS_PER_DOMAIN,
        "excluded_question_ids_by_domain": {domain: sorted(excluded[domain]) for domain in DOMAINS},
        "available_question_type_counts_by_domain": available_counts,
        "selected_question_type_counts_by_domain": selected_counts,
        "question_ids_by_domain": selected_ids,
        "question_types_by_domain": selected_types,
    }


def allocate_stratum_quotas(counts: dict[str, int], *, sample_size: int) -> dict[str, int]:
    if not counts or any(count < 1 for count in counts.values()):
        raise ValueError("Holdout strata must be non-empty")
    if len(counts) > sample_size or sum(counts.values()) < sample_size:
        raise ValueError("Holdout strata cannot satisfy the requested sample size")
    quotas = dict.fromkeys(counts, 1)
    remaining = sample_size - len(counts)
    capacity = {name: count - 1 for name, count in counts.items()}
    capacity_total = sum(capacity.values())
    raw = {
        name: (remaining * available / capacity_total if capacity_total else 0.0)
        for name, available in capacity.items()
    }
    for name, value in raw.items():
        quotas[name] += int(value)
    unallocated = sample_size - sum(quotas.values())
    order = sorted(
        counts,
        key=lambda name: (
            -(raw[name] - int(raw[name])),
            stable_sample_key(domain="quota", question_type=name, question_id=name),
        ),
    )
    for name in order[:unallocated]:
        quotas[name] += 1
    if any(quotas[name] > counts[name] for name in counts):
        raise ValueError("Holdout allocation exceeded a stratum capacity")
    return quotas


def stable_sample_key(*, domain: str, question_type: str, question_id: str) -> str:
    value = f"{HOLDOUT_SAMPLE_SEED}:{domain}:{question_type}:{question_id}"
    return hashlib.sha256(value.encode()).hexdigest()


def holdout_cost_budget(replication_report: dict[str, Any]) -> dict[str, Any]:
    configurations = replication_report.get("configurations")
    costs = replication_report.get("costs")
    if not isinstance(configurations, dict) or not isinstance(costs, dict):
        raise TypeError("Replication report is missing cost evidence")
    pilot_question_runs = 0
    for configuration in PRIMARY_CONFIGURATIONS:
        summary = configurations.get(configuration)
        if not isinstance(summary, dict):
            raise TypeError("Replication report is missing a primary configuration")
        aggregate = summary.get("aggregate")
        if not isinstance(aggregate, dict):
            raise TypeError("Replication report is missing an aggregate")
        pilot_question_runs += int(aggregate["question_count_per_pass"]) * PASS_COUNT
    pilot_cost = float(costs["total_model_cost_usd"])
    pilot_cost_per_question_run = pilot_cost / pilot_question_runs
    holdout_question_runs = TOTAL_QUESTION_COUNT * len(HOLDOUT_CONFIGURATIONS) * PASS_COUNT
    estimated = pilot_cost_per_question_run * holdout_question_runs
    if estimated > MAX_TOTAL_MODEL_COST_USD:
        raise ValueError("Reader holdout estimate exceeds the total model cost budget")
    return {
        "pilot_model_cost_usd": pilot_cost,
        "pilot_question_runs": pilot_question_runs,
        "pilot_cost_per_question_run_usd": pilot_cost_per_question_run,
        "holdout_question_runs": holdout_question_runs,
        "estimated_total_model_cost_usd": estimated,
        "max_total_model_cost_usd": MAX_TOTAL_MODEL_COST_USD,
        "max_pilot_cost_multiplier": MAX_PILOT_COST_MULTIPLIER,
        "basis": "provider-reported reader and judge cost from the five-pass pilot",
    }


def derive_holdout_command(
    source: list[str],
    *,
    output_dir: Path,
    question_ids: list[str],
    shuffle_seed: int | None,
    context_expansion_max_ratio: float,
) -> list[str]:
    command = replace_option(source, "--output-dir", str(output_dir))
    command = replace_multi_option(command, "--question-ids", question_ids)
    command = set_option(
        command,
        "--context-expansion-max-ratio",
        str(context_expansion_max_ratio),
    )
    command = remove_option(command, "--shuffle-questions-seed")
    if shuffle_seed is not None:
        command = set_option(command, "--shuffle-questions-seed", str(shuffle_seed))
    return command


def replace_multi_option(command: list[str], flag: str, values: list[str]) -> list[str]:
    if not values:
        raise ValueError(f"Reader command replacement for {flag} is empty")
    result = list(command)
    try:
        start = result.index(flag) + 1
    except ValueError as exc:
        raise ValueError(f"Reader command is missing {flag}") from exc
    end = start
    while end < len(result) and not result[end].startswith("--"):
        end += 1
    if start == end:
        raise ValueError(f"Reader command has no values for {flag}")
    result[start:end] = values
    return result


def remove_option(command: list[str], flag: str) -> list[str]:
    result = list(command)
    if flag not in result:
        return result
    index = result.index(flag)
    if index + 1 >= len(result) or result[index + 1].startswith("--"):
        raise ValueError(f"Reader command has no value for {flag}")
    del result[index : index + 2]
    return result


def source_memory_artifacts(
    commands: dict[tuple[str, str], list[str]],
) -> dict[str, Any]:
    result = {}
    for source_configuration in PRIMARY_CONFIGURATIONS:
        result[source_configuration] = {}
        for domain in DOMAINS:
            memory_dir = Path(
                expected_run_config(commands[(source_configuration, domain)])["load_memory_dir"]
            ).resolve()
            result[source_configuration][domain] = {
                "path": str(memory_dir),
                "sha256": memory_artifact_sha256(memory_dir),
            }
    return result


def memory_artifact_sha256(memory_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in MEMORY_ARTIFACTS:
        path = memory_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"Saved memory artifact is missing {path}")
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def require_reader_holdout_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if plan.get("schema_version") != HOLDOUT_PLAN_SCHEMA_VERSION:
        raise ValueError("Invalid reader holdout plan")
    if plan.get("configurations") != list(HOLDOUT_CONFIGURATIONS):
        raise ValueError("Reader holdout plan changed its configurations")
    require_holdout_protocol(plan.get("protocol"))
    require_holdout_analysis(plan.get("analysis"))
    require_holdout_integrity(plan.get("integrity_contract"))
    require_holdout_selection(plan.get("selection"))
    require_holdout_cost_budget(plan.get("cost_budget"))
    runs = plan.get("runs")
    if not isinstance(runs, list):
        raise TypeError("Reader holdout plan has no runs")
    validate_holdout_runs(runs, plan=plan)
    validate_holdout_sources(plan)
    return runs


def require_holdout_protocol(raw: Any) -> None:
    expected = {
        "passes_per_configuration": PASS_COUNT,
        "question_order_seeds": list(PASS_SEEDS),
        "question_count": TOTAL_QUESTION_COUNT,
        "questions_per_domain": QUESTIONS_PER_DOMAIN,
        "fixed_sample_no_sequential_stopping": True,
        "all_runs_are_fresh": True,
        "execution_wave": "pass",
        "max_workers_cap": DEFAULT_MAX_WORKERS,
        "score_visibility_during_execution": "none",
    }
    if raw != expected:
        raise ValueError("Reader holdout plan changed its fixed protocol")


def require_holdout_analysis(raw: Any) -> None:
    expected = {
        "unit": "question",
        "passes_required": PASS_COUNT,
        "primary_contrast": "winner_token_bounded_1_2x_vs_baseline_fixed_reader",
        "secondary_contrasts": [
            "winner_fixed_reader_vs_baseline_fixed_reader",
            "winner_fixed_reader_vs_winner_token_bounded_1_2x",
        ],
        "go_min_mean_accuracy_delta": 0.03,
        "go_min_probability_positive": 0.85,
        "go_min_confidence_interval_lower": -0.02,
        "go_max_memory_token_inflation": 0.25,
        "candidate_selection_min_delta_over_bounded": 0.03,
        "minimum_bounded_binding_rate": 0.15,
        "no_go_max_probability_positive": 0.5,
        "otherwise": "RESEARCH-MORE",
    }
    if raw != expected:
        raise ValueError("Reader holdout plan changed its analysis contract")


def require_holdout_integrity(raw: Any) -> None:
    expected = {
        "sampling_inputs": ["question_id", "domain", "question_type"],
        "sampling_uses_question_text": False,
        "sampling_uses_answers_or_scores": False,
        "configuration_selection_uses_holdout_scores": False,
        "all_passes_required_for_decision": True,
        "interim_score_inspection_allowed": False,
        "memory_builds_reused": True,
        "claim_level": "question-level holdout on shared small-tier domain haystacks",
    }
    if raw != expected:
        raise ValueError("Reader holdout plan changed its integrity contract")


def require_holdout_selection(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise TypeError("Reader holdout plan has no selection record")
    if (
        raw.get("algorithm")
        != "minimum-one stratified largest-remainder allocation with SHA-256 ranking"
        or raw.get("seed") != HOLDOUT_SAMPLE_SEED
        or raw.get("questions_per_domain") != QUESTIONS_PER_DOMAIN
    ):
        raise ValueError("Reader holdout plan changed its selection protocol")
    question_ids = raw.get("question_ids_by_domain")
    question_types = raw.get("question_types_by_domain")
    excluded = raw.get("excluded_question_ids_by_domain")
    if not all(isinstance(item, dict) for item in (question_ids, question_types, excluded)):
        raise TypeError("Reader holdout selection is incomplete")
    for domain in DOMAINS:
        ids = question_ids.get(domain)
        types = question_types.get(domain)
        excluded_ids = excluded.get(domain)
        if (
            not isinstance(ids, list)
            or len(ids) != QUESTIONS_PER_DOMAIN
            or len(ids) != len(set(ids))
            or ids != sorted(ids)
            or not isinstance(types, dict)
            or set(types) != set(ids)
            or not isinstance(excluded_ids, list)
            or set(ids) & set(excluded_ids)
        ):
            raise ValueError(f"Reader holdout selection is invalid for {domain}")


def require_holdout_cost_budget(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise TypeError("Reader holdout plan has no cost budget")
    if (
        raw.get("holdout_question_runs")
        != TOTAL_QUESTION_COUNT * len(HOLDOUT_CONFIGURATIONS) * PASS_COUNT
        or raw.get("max_total_model_cost_usd") != MAX_TOTAL_MODEL_COST_USD
        or raw.get("max_pilot_cost_multiplier") != MAX_PILOT_COST_MULTIPLIER
        or float(raw.get("estimated_total_model_cost_usd", -1.0)) > MAX_TOTAL_MODEL_COST_USD
        or raw.get("basis") != "provider-reported reader and judge cost from the five-pass pilot"
    ):
        raise ValueError("Reader holdout plan changed its cost budget")


def validate_holdout_runs(runs: list[Any], *, plan: dict[str, Any]) -> None:
    expected_keys = {
        (pass_id(index), configuration, domain)
        for index in range(1, PASS_COUNT + 1)
        for configuration in HOLDOUT_CONFIGURATIONS
        for domain in DOMAINS
    }
    actual_keys = set()
    identities = {}
    output_root = Path(plan["output_root"])
    selected_ids = plan["selection"]["question_ids_by_domain"]
    for raw in runs:
        if not isinstance(raw, dict) or not isinstance(raw.get("command"), list):
            raise TypeError("Reader holdout plan contains an invalid run")
        key = (str(raw.get("pass_id")), str(raw.get("configuration")), str(raw.get("domain")))
        if key in actual_keys:
            raise ValueError(f"Duplicate reader holdout run: {key}")
        actual_keys.add(key)
        pass_index = int(raw["pass_index"])
        configuration = str(raw["configuration"])
        domain = str(raw["domain"])
        seed = PASS_SEEDS[pass_index - 1]
        expected = expected_run_config(raw["command"])
        expected_output = output_root / "runs" / pass_id(pass_index) / configuration / domain
        if (
            tuple(raw["command"][: len(COMMAND_PREFIX)]) != COMMAND_PREFIX
            or "--allow-localhost" not in raw["command"]
            or raw["pass_id"] != pass_id(pass_index)
            or raw.get("shuffle_questions_seed") != seed
            or expected["shuffle_questions_seed"] != seed
            or expected["question_ids"] != selected_ids[domain]
            or Path(raw["output_dir"]).resolve() != expected_output.resolve()
            or Path(expected["output_dir"]).resolve() != expected_output.resolve()
        ):
            raise ValueError(f"Reader holdout run changed its frozen identity: {key}")
        expected_ratio = (
            TOKEN_BOUNDED_MAX_RATIO if configuration == "winner_token_bounded_1_2x" else 0.0
        )
        if expected["context_expansion_max_ratio"] != expected_ratio:
            raise ValueError("Reader holdout run changed its expansion budget")
        identity = {
            name: value
            for name, value in expected.items()
            if name not in {"output_dir", "shuffle_questions_seed"}
        }
        identity_key = (configuration, domain)
        if identity_key in identities and identities[identity_key] != identity:
            raise ValueError("Reader holdout changed a configuration between passes")
        identities[identity_key] = identity
    if actual_keys != expected_keys:
        raise ValueError("Reader holdout plan does not cover every pass and domain")


def validate_holdout_sources(plan: dict[str, Any]) -> None:
    sources = plan.get("source_artifacts")
    if not isinstance(sources, dict):
        raise TypeError("Reader holdout plan has no source artifacts")
    for name in ("replication_plan", "replication_report", "questions"):
        source = sources.get(name)
        if not isinstance(source, dict):
            raise TypeError(f"Reader holdout plan has no {name} binding")
        path = Path(str(source.get("path") or "")).resolve()
        if not path.is_file() or source.get("sha256") != sha256_file(path):
            raise ValueError(f"Reader holdout {name} binding changed")
    replication_plan = load_json(Path(sources["replication_plan"]["path"]))
    source_commands = source_command_map(require_reader_replication_plan(replication_plan))
    excluded_ids = excluded_question_ids(source_commands)
    if excluded_ids != plan["selection"]["excluded_question_ids_by_domain"]:
        raise ValueError("Reader holdout diagnostic exclusions changed")
    questions_path = shared_data_root(source_commands) / "questions.jsonl"
    if questions_path != Path(sources["questions"]["path"]):
        raise ValueError("Reader holdout questions source changed")
    if select_holdout_questions(questions_path, excluded_ids=excluded_ids) != plan["selection"]:
        raise ValueError("Reader holdout deterministic selection changed")
    expected_memory = source_memory_artifacts(source_commands)
    if sources.get("memory") != expected_memory:
        raise ValueError("Reader holdout memory artifact binding changed")
    replication_report = load_json(Path(sources["replication_report"]["path"]))
    validate_replication_report_source(
        replication_report,
        replication_plan_path=Path(sources["replication_plan"]["path"]),
    )
    if holdout_cost_budget(replication_report) != plan["cost_budget"]:
        raise ValueError("Reader holdout cost budget changed")
    validate_derived_holdout_commands(plan, source_commands=source_commands)


def validate_derived_holdout_commands(
    plan: dict[str, Any],
    *,
    source_commands: dict[tuple[str, str], list[str]],
) -> None:
    selected_ids = plan["selection"]["question_ids_by_domain"]
    for run in plan["runs"]:
        configuration = str(run["configuration"])
        domain = str(run["domain"])
        expected = derive_holdout_command(
            source_commands[(SOURCE_CONFIGURATION[configuration], domain)],
            output_dir=Path(run["output_dir"]),
            question_ids=selected_ids[domain],
            shuffle_seed=run["shuffle_questions_seed"],
            context_expansion_max_ratio=(
                TOKEN_BOUNDED_MAX_RATIO if configuration == "winner_token_bounded_1_2x" else 0.0
            ),
        )
        if run["command"] != expected:
            raise ValueError("Reader holdout command changed from its bound source")


def run_reader_holdout_plan(
    plan: dict[str, Any],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    if max_workers < 1:
        raise ValueError("Reader holdout workers must be positive")
    runs = require_reader_holdout_plan(plan)
    if max_workers > plan["protocol"]["max_workers_cap"]:
        raise ValueError("Reader holdout workers exceed the predeclared cap")
    completed = []
    skipped = []
    failures = []
    model_cost = 0.0
    question_runs = 0
    run_cost_per_question = []
    for index in range(1, PASS_COUNT + 1):
        wave = [run for run in runs if run["pass_index"] == index]
        pending = []
        for run in wave:
            summary = completed_run_cost(run)
            if summary is None:
                pending.append(run)
            else:
                skipped.append(run_key(run))
                model_cost += summary["model_cost_usd"]
                question_runs += summary["question_count"]
                run_cost_per_question.append(summary["cost_per_question_run_usd"])
        for run, returncode in execute_wave(pending, max_workers=max_workers):
            if returncode != 0:
                failures.append({"run": run_key(run), "returncode": returncode})
                continue
            summary = completed_run_cost(run)
            if summary is None:
                failures.append({"run": run_key(run), "returncode": 0, "receipt_valid": False})
                continue
            completed.append(run_key(run))
            model_cost += summary["model_cost_usd"]
            question_runs += summary["question_count"]
            run_cost_per_question.append(summary["cost_per_question_run_usd"])
        failures.extend(
            cost_failures(
                plan,
                model_cost=model_cost,
                run_cost_per_question=run_cost_per_question,
            )
        )
        if failures:
            break
    return {
        "status": "PASS" if not failures else "FAIL",
        "completed": completed,
        "skipped": skipped,
        "failures": failures,
        "model_cost_usd": model_cost,
        "question_runs": question_runs,
        "max_workers": max_workers,
        "scores_read": False,
    }


def completed_run_cost(run: dict[str, Any]) -> dict[str, Any] | None:
    run_dir = Path(run["output_dir"])
    expected = expected_run_config(run["command"])
    try:
        require_reader_run_files(run_dir)
        receipt = load_json(run_dir / "longmemeval_v2_official_receipt.json")
        run_args = load_json(run_dir / "run_args.json")
        reader_model, evaluator_model = validate_reader_run_identity(
            run_dir=run_dir,
            expected=expected,
            receipt=receipt,
            run_args=run_args,
        )
        require_completed_run_provenance(
            expected=expected,
            receipt=receipt,
            reader_model=reader_model,
            evaluator_model=evaluator_model,
        )
        model_cost = reader_model_cost(run_dir=run_dir, receipt=receipt)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None
    return {
        "model_cost_usd": model_cost,
        "question_count": len(expected["question_ids"]),
        "cost_per_question_run_usd": model_cost / len(expected["question_ids"]),
    }


def require_completed_run_provenance(
    *,
    expected: dict[str, Any],
    receipt: dict[str, Any],
    reader_model: str,
    evaluator_model: str,
) -> None:
    if not reader_model or not evaluator_model:
        raise ValueError("Reader holdout run is missing model pins")
    dataset = receipt.get("dataset")
    if not isinstance(dataset, dict) or dataset.get(
        "selected_question_ids_sha256"
    ) != sha256_question_ids(expected["question_ids"]):
        raise ValueError("Reader holdout run changed its selected question identity")


def cost_failures(
    plan: dict[str, Any],
    *,
    model_cost: float,
    run_cost_per_question: list[float],
) -> list[dict[str, Any]]:
    budget = plan["cost_budget"]
    failures = []
    if model_cost > budget["max_total_model_cost_usd"]:
        failures.append(
            {
                "cost_budget_exceeded": True,
                "model_cost_usd": model_cost,
            }
        )
    pilot = budget["pilot_cost_per_question_run_usd"]
    maximum_run_cost = max(run_cost_per_question, default=0.0)
    if maximum_run_cost > pilot * budget["max_pilot_cost_multiplier"]:
        failures.append(
            {
                "pilot_cost_multiplier_exceeded": True,
                "max_run_cost_per_question_usd": maximum_run_cost,
            }
        )
    return failures


def execute_wave(
    runs: list[dict[str, Any]],
    *,
    max_workers: int,
) -> list[tuple[dict[str, Any], int]]:
    if not runs:
        return []
    results = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(runs))) as executor:
        futures = {executor.submit(execute_run, run): run for run in runs}
        for future in as_completed(futures):
            results.append((futures[future], future.result()))
    return results


def execute_run(run: dict[str, Any]) -> int:
    output_dir = Path(run["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "holdout_runner.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps({"event": "start", "run": run_key(run)}) + "\n")
        log.flush()
        (output_dir / "exit_code").unlink(missing_ok=True)
        (output_dir / "exit_code.tmp").unlink(missing_ok=True)
        result = subprocess.run(  # noqa: S603
            run["command"],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        write_exit_code(output_dir, result.returncode)
        log.write(
            json.dumps({"event": "complete", "run": run_key(run), "returncode": result.returncode})
            + "\n"
        )
    return result.returncode


def write_exit_code(output_dir: Path, returncode: int) -> None:
    pending_path = output_dir / "exit_code.tmp"
    pending_path.write_text(f"{returncode}\n", encoding="utf-8")
    pending_path.replace(output_dir / "exit_code")


def run_key(run: dict[str, Any]) -> str:
    return f"{run['pass_id']}:{run['configuration']}:{run['domain']}"
