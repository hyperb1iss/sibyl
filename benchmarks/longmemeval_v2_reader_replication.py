"""Plan and execute receipt-bound LongMemEval-V2 reader replications."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from math import isclose
from pathlib import Path
from typing import Any

from benchmarks.longmemeval_v2_reader_report import (
    DOMAINS,
    REQUIRED_RUN_FILES,
    expected_run_config,
    load_json,
    load_reader_run,
    planned_runs,
    reader_input_identity,
    reader_model_cost,
    require_reader_plan,
    require_reader_run_files,
    sha256_file,
    validate_reader_run_identity,
)

ROOT = Path(__file__).resolve().parents[1]
REPLICATION_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-replication-plan-v1"
PRIMARY_CONFIGURATIONS = ("baseline_fixed_reader", "winner_fixed_reader")
PASS_SEEDS = (None, 1729, 2718, 3141, 5772)
PASS_COUNT = len(PASS_SEEDS)
DEFAULT_MAX_WORKERS = 4
MAX_INCREMENTAL_MODEL_COST_USD = 2.0
BOOTSTRAP_SAMPLES = 20_000
BOOTSTRAP_SEED = 20_260_713
MIN_GO_ACCURACY_DELTA = 0.03
MAX_GO_DOMAIN_REGRESSION = 0.0
MAX_NO_GO_ACCURACY_DELTA = -0.03
RUNTIME_FLAGS = (
    ("--reader-base-url", "base_url"),
    ("--reader-api-key-env", "api_key_env"),
    ("--reader-max-concurrent-requests", "reader_max_concurrent_requests"),
    ("--max-completion-tokens", "max_completion_tokens"),
    ("--timeout-seconds", "timeout_seconds"),
    ("--evaluator-model", "evaluator_model"),
    ("--evaluator-base-url", "evaluator_base_url"),
    ("--evaluator-api-key-env", "evaluator_api_key_env"),
    ("--evaluator-reasoning-effort", "evaluator_reasoning_effort"),
    ("--evaluator-max-completion-tokens", "evaluator_max_completion_tokens"),
    ("--evaluator-timeout-seconds", "evaluator_timeout_seconds"),
    ("--prompt-build-max-workers", "prompt_build_max_workers"),
)
COMMAND_PREFIX = ("moon", "run", "root:bench-longmemeval-v2-official-full", "--")


def build_reader_replication_plan(
    *,
    reader_plan: dict[str, Any],
    reader_plan_path: Path,
    source_run_roots: dict[str, Path],
    output_root: Path,
) -> dict[str, Any]:
    reader_plan_path = reader_plan_path.expanduser().resolve()
    if load_json(reader_plan_path) != reader_plan:
        raise ValueError("Reader plan path does not match the loaded reader plan")
    configurations = require_reader_plan(reader_plan)
    if tuple(configurations[:2]) != PRIMARY_CONFIGURATIONS:
        raise ValueError("Reader plan does not expose the primary replication contrast")
    if set(source_run_roots) != set(PRIMARY_CONFIGURATIONS):
        raise ValueError("Replication source runs must include baseline and winner")

    commands = reader_command_map(reader_plan)
    runs = []
    frozen_commands = {}
    source_runs: dict[str, dict[str, Any]] = {}
    existing_model_cost = 0.0
    runtime_fingerprints = set()
    for configuration in PRIMARY_CONFIGURATIONS:
        source_runs[configuration] = {}
        for domain in DOMAINS:
            command = commands[(configuration, domain)]
            run_dir = source_run_roots[configuration].expanduser().resolve() / domain
            model_cost, source = load_replication_source(
                run_dir=run_dir,
                expected=expected_run_config(command),
            )
            command, runtime_config = freeze_reader_command(command, run_dir=run_dir)
            runtime_fingerprints.add(json.dumps(runtime_config, sort_keys=True))
            load_replication_source(run_dir=run_dir, expected=expected_run_config(command))
            frozen_commands[(configuration, domain)] = command
            existing_model_cost += model_cost
            source_runs[configuration][domain] = source
            runs.append(
                replication_run(
                    pass_index=1,
                    configuration=configuration,
                    domain=domain,
                    command=command,
                    existing=True,
                )
            )

    if len(runtime_fingerprints) != 1:
        raise ValueError("Reader source runs do not share one runtime configuration")
    estimated_incremental_model_cost = existing_model_cost * (PASS_COUNT - 1)
    if estimated_incremental_model_cost > MAX_INCREMENTAL_MODEL_COST_USD:
        raise ValueError("Reader replication estimate exceeds the incremental cost budget")

    output_root = output_root.expanduser().resolve()
    for pass_index, seed in enumerate(PASS_SEEDS[1:], start=2):
        for configuration in PRIMARY_CONFIGURATIONS:
            for domain in DOMAINS:
                output_dir = output_root / "runs" / pass_id(pass_index) / configuration / domain
                command = replace_option(
                    frozen_commands[(configuration, domain)],
                    "--output-dir",
                    str(output_dir),
                )
                command = set_option(command, "--shuffle-questions-seed", str(seed))
                runs.append(
                    replication_run(
                        pass_index=pass_index,
                        configuration=configuration,
                        domain=domain,
                        command=command,
                        existing=False,
                    )
                )

    return {
        "schema_version": REPLICATION_PLAN_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "output_root": str(output_root),
        "configurations": list(PRIMARY_CONFIGURATIONS),
        "protocol": {
            "passes_per_configuration": PASS_COUNT,
            "question_order_seeds": list(PASS_SEEDS),
            "existing_passes": 1,
            "new_passes": PASS_COUNT - 1,
            "fixed_sample_no_sequential_stopping": True,
            "selection_basis": "pre-registered primary reader contrast",
            "execution_wave": "pass",
            "max_workers_cap": DEFAULT_MAX_WORKERS,
        },
        "analysis": {
            "unit": "question",
            "cluster_bootstrap_samples": BOOTSTRAP_SAMPLES,
            "cluster_bootstrap_seed": BOOTSTRAP_SEED,
            "confidence_level": 0.95,
            "decision_rule": {
                "go_min_mean_accuracy_delta": MIN_GO_ACCURACY_DELTA,
                "go_max_domain_regression": MAX_GO_DOMAIN_REGRESSION,
                "go_requires_positive_ci_lower_bound": True,
                "no_go_max_mean_accuracy_delta": MAX_NO_GO_ACCURACY_DELTA,
                "no_go_if_mean_threshold_or_negative_ci_upper_bound": True,
                "otherwise": "RESEARCH-MORE",
            },
        },
        "cost_budget": {
            "existing_model_cost_usd": existing_model_cost,
            "estimated_incremental_model_cost_usd": estimated_incremental_model_cost,
            "max_incremental_model_cost_usd": MAX_INCREMENTAL_MODEL_COST_USD,
            "basis": "provider-reported reader and judge cost from pass one",
        },
        "integrity_contract": {
            "configuration_selection_uses_reader_scores": False,
            "planning_uses_official_scoring_outputs": False,
            "candidate_generation_inputs": ["question", "image"],
            "all_passes_required_for_decision": True,
            "memory_builds_reused": True,
        },
        "runs": runs,
        "source_artifacts": {
            "reader_plan": {
                "path": str(reader_plan_path),
                "sha256": sha256_file(reader_plan_path),
            },
            "existing_runs": source_runs,
        },
    }


def load_replication_source(
    *,
    run_dir: Path,
    expected: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    run_dir = run_dir.resolve()
    require_reader_run_files(run_dir)
    receipt = load_json(run_dir / "longmemeval_v2_official_receipt.json")
    run_args = load_json(run_dir / "run_args.json")
    reader_model, evaluator_model = validate_reader_run_identity(
        run_dir=run_dir,
        expected=expected,
        receipt=receipt,
        run_args=run_args,
    )
    if not reader_model or not evaluator_model:
        raise ValueError(f"Reader source run is missing model pins: {run_dir}")
    source = {
        "path": str(run_dir),
        "input_identity": reader_input_identity(
            receipt=receipt,
            expected=expected,
            run_dir=run_dir,
        ),
        "artifacts": {
            name: {"path": str(run_dir / name), "sha256": sha256_file(run_dir / name)}
            for name in REQUIRED_RUN_FILES
        },
    }
    return reader_model_cost(run_dir=run_dir, receipt=receipt), source


def reader_command_map(reader_plan: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    planned_runs(reader_plan)
    result = {}
    for item in reader_plan["commands"]:
        key = (str(item["configuration"]), str(item["domain"]))
        result[key] = [str(value) for value in item["command"]]
    return result


def replication_run(
    *,
    pass_index: int,
    configuration: str,
    domain: str,
    command: list[str],
    existing: bool,
) -> dict[str, Any]:
    expected = expected_run_config(command)
    return {
        "pass_id": pass_id(pass_index),
        "pass_index": pass_index,
        "configuration": configuration,
        "domain": domain,
        "existing": existing,
        "shuffle_questions_seed": expected["shuffle_questions_seed"],
        "output_dir": expected["output_dir"],
        "command": command,
    }


def freeze_reader_command(
    command: list[str],
    *,
    run_dir: Path,
) -> tuple[list[str], dict[str, Any]]:
    run_args = load_json(run_dir / "run_args.json")
    runtime_config = {}
    result = list(command)
    for flag, key in RUNTIME_FLAGS:
        value = run_args.get(key)
        if value is None:
            raise ValueError(f"Reader source run is missing runtime option {key}: {run_dir}")
        runtime_config[key] = value
        result = set_option(result, flag, str(value))
    return result, runtime_config


def pass_id(pass_index: int) -> str:
    return f"pass_{pass_index:02d}"


def replace_option(command: list[str], flag: str, value: str) -> list[str]:
    result = list(command)
    try:
        index = result.index(flag)
    except ValueError as exc:
        raise ValueError(f"Reader command is missing {flag}") from exc
    if index + 1 >= len(result) or result[index + 1].startswith("--"):
        raise ValueError(f"Reader command has no value for {flag}")
    result[index + 1] = value
    return result


def set_option(command: list[str], flag: str, value: str) -> list[str]:
    if flag in command:
        return replace_option(command, flag, value)
    result = list(command)
    insertion = result.index("--allow-localhost") if "--allow-localhost" in result else len(result)
    result[insertion:insertion] = [flag, value]
    return result


def require_reader_replication_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if plan.get("schema_version") != REPLICATION_PLAN_SCHEMA_VERSION:
        raise ValueError("Invalid reader replication plan")
    if plan.get("configurations") != list(PRIMARY_CONFIGURATIONS):
        raise ValueError("Reader replication plan changed the primary contrast")
    protocol = plan.get("protocol")
    if not isinstance(protocol, dict):
        raise TypeError("Reader replication plan has no protocol")
    if protocol != {
        "passes_per_configuration": PASS_COUNT,
        "question_order_seeds": list(PASS_SEEDS),
        "existing_passes": 1,
        "new_passes": PASS_COUNT - 1,
        "fixed_sample_no_sequential_stopping": True,
        "selection_basis": "pre-registered primary reader contrast",
        "execution_wave": "pass",
        "max_workers_cap": DEFAULT_MAX_WORKERS,
    }:
        raise ValueError("Reader replication plan changed its fixed protocol")
    if plan.get("analysis") != {
        "unit": "question",
        "cluster_bootstrap_samples": BOOTSTRAP_SAMPLES,
        "cluster_bootstrap_seed": BOOTSTRAP_SEED,
        "confidence_level": 0.95,
        "decision_rule": {
            "go_min_mean_accuracy_delta": MIN_GO_ACCURACY_DELTA,
            "go_max_domain_regression": MAX_GO_DOMAIN_REGRESSION,
            "go_requires_positive_ci_lower_bound": True,
            "no_go_max_mean_accuracy_delta": MAX_NO_GO_ACCURACY_DELTA,
            "no_go_if_mean_threshold_or_negative_ci_upper_bound": True,
            "otherwise": "RESEARCH-MORE",
        },
    }:
        raise ValueError("Reader replication plan changed its analysis contract")
    if plan.get("integrity_contract") != {
        "configuration_selection_uses_reader_scores": False,
        "planning_uses_official_scoring_outputs": False,
        "candidate_generation_inputs": ["question", "image"],
        "all_passes_required_for_decision": True,
        "memory_builds_reused": True,
    }:
        raise ValueError("Reader replication plan changed its integrity contract")
    validate_cost_budget(plan.get("cost_budget"))

    runs = plan.get("runs")
    if not isinstance(runs, list):
        raise TypeError("Reader replication plan has no runs")
    validate_replication_runs(runs, output_root=Path(str(plan.get("output_root") or "")))
    validate_source_artifacts(plan, runs=runs)
    return runs


def validate_cost_budget(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise TypeError("Reader replication plan has no cost budget")
    existing = float(raw.get("existing_model_cost_usd", -1.0))
    estimate = float(raw.get("estimated_incremental_model_cost_usd", -1.0))
    if (
        existing < 0.0
        or not isclose(estimate, existing * (PASS_COUNT - 1), abs_tol=1e-12)
        or raw.get("max_incremental_model_cost_usd") != MAX_INCREMENTAL_MODEL_COST_USD
        or estimate > MAX_INCREMENTAL_MODEL_COST_USD
        or raw.get("basis") != "provider-reported reader and judge cost from pass one"
    ):
        raise ValueError("Reader replication plan changed its cost budget")


def validate_replication_runs(runs: list[Any], *, output_root: Path) -> None:
    expected_keys = {
        (pass_id(index), configuration, domain)
        for index in range(1, PASS_COUNT + 1)
        for configuration in PRIMARY_CONFIGURATIONS
        for domain in DOMAINS
    }
    actual_keys = set()
    identities: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in runs:
        if not isinstance(raw, dict) or not isinstance(raw.get("command"), list):
            raise TypeError("Reader replication plan contains an invalid run")
        key = (str(raw.get("pass_id")), str(raw.get("configuration")), str(raw.get("domain")))
        if key in actual_keys:
            raise ValueError(f"Duplicate reader replication run: {key}")
        actual_keys.add(key)
        validate_replication_run(raw, identities=identities, output_root=output_root)
    if actual_keys != expected_keys:
        raise ValueError("Reader replication plan does not cover every pass and domain")


def validate_replication_run(
    run: dict[str, Any],
    *,
    identities: dict[tuple[str, str], dict[str, Any]],
    output_root: Path,
) -> None:
    pass_index = int(run["pass_index"])
    seed = PASS_SEEDS[pass_index - 1]
    if run["pass_id"] != pass_id(pass_index) or run.get("shuffle_questions_seed") != seed:
        raise ValueError("Reader replication run has inconsistent pass identity")
    if bool(run.get("existing")) != (pass_index == 1):
        raise ValueError("Only the first reader replication pass may be existing")
    expected = expected_run_config(run["command"])
    if tuple(run["command"][: len(COMMAND_PREFIX)]) != COMMAND_PREFIX:
        raise ValueError("Reader replication command changed its executable")
    if "--allow-localhost" not in run["command"]:
        raise ValueError("Reader replication command changed its isolated runtime policy")
    if expected["shuffle_questions_seed"] != seed:
        raise ValueError("Reader replication command changed its question order")
    if Path(expected["output_dir"]).resolve() != Path(run["output_dir"]).resolve():
        raise ValueError("Reader replication command changed its output directory")
    if pass_index > 1:
        expected_output = output_root / "runs" / pass_id(pass_index) / config_key_path(run)
        if Path(run["output_dir"]).resolve() != expected_output.resolve():
            raise ValueError("Reader replication run escaped its output root")
    identity = {
        key: value
        for key, value in expected.items()
        if key not in {"output_dir", "shuffle_questions_seed"}
    }
    config_key = (str(run["configuration"]), str(run["domain"]))
    if config_key in identities and identities[config_key] != identity:
        raise ValueError("Reader replication changed a frozen run configuration")
    identities[config_key] = identity


def config_key_path(run: dict[str, Any]) -> Path:
    return Path(str(run["configuration"])) / str(run["domain"])


def validate_source_artifacts(plan: dict[str, Any], *, runs: list[dict[str, Any]]) -> None:
    source_artifacts = plan.get("source_artifacts")
    if not isinstance(source_artifacts, dict):
        raise TypeError("Reader replication plan has no source artifacts")
    existing_runs = source_artifacts.get("existing_runs")
    if not isinstance(existing_runs, dict):
        raise TypeError("Reader replication plan has no existing run bindings")
    reader_plan_source = source_artifacts.get("reader_plan")
    if not isinstance(reader_plan_source, dict):
        raise TypeError("Reader replication plan has no reader plan binding")
    reader_plan_path = Path(str(reader_plan_source.get("path") or "")).resolve()
    if not reader_plan_path.is_file() or reader_plan_source.get("sha256") != sha256_file(
        reader_plan_path
    ):
        raise ValueError("Reader replication reader plan binding changed")
    commands = reader_command_map(load_json(reader_plan_path))
    frozen_commands = {}
    for run in runs:
        if not run["existing"]:
            continue
        try:
            source = existing_runs[run["configuration"]][run["domain"]]
        except (KeyError, TypeError) as exc:
            raise ValueError("Reader replication plan has incomplete source bindings") from exc
        if (
            not isinstance(source, dict)
            or Path(str(source.get("path") or "")).resolve() != Path(run["output_dir"]).resolve()
        ):
            raise ValueError("Reader replication plan changed an existing run binding")
        frozen_command, _ = freeze_reader_command(
            commands[(run["configuration"], run["domain"])],
            run_dir=Path(run["output_dir"]),
        )
        if run["command"] != frozen_command:
            raise ValueError("Reader replication changed its bound source command")
        frozen_commands[(run["configuration"], run["domain"])] = frozen_command
    for run in runs:
        if run["existing"]:
            continue
        expected_command = replace_option(
            frozen_commands[(run["configuration"], run["domain"])],
            "--output-dir",
            run["output_dir"],
        )
        expected_command = set_option(
            expected_command,
            "--shuffle-questions-seed",
            str(run["shuffle_questions_seed"]),
        )
        if run["command"] != expected_command:
            raise ValueError("Reader replication changed its derived command")


def run_reader_replication_plan(
    plan: dict[str, Any],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    if max_workers < 1:
        raise ValueError("Reader replication workers must be positive")
    runs = require_reader_replication_plan(plan)
    if max_workers > plan["protocol"]["max_workers_cap"]:
        raise ValueError("Reader replication workers exceed the predeclared cap")
    validate_existing_runs(plan, runs=runs)
    completed = []
    skipped = []
    failures = []
    model_cost = 0.0
    for index in range(2, PASS_COUNT + 1):
        wave = [run for run in runs if run["pass_index"] == index]
        pending = []
        for run in wave:
            summary = completed_run_summary(run)
            if summary is None:
                pending.append(run)
            else:
                skipped.append(run_key(run))
                model_cost += summary["model_cost_usd"]
        results = execute_wave(pending, max_workers=max_workers)
        for run, returncode in results:
            if returncode != 0:
                failures.append({"run": run_key(run), "returncode": returncode})
                continue
            summary = completed_run_summary(run)
            if summary is None:
                failures.append({"run": run_key(run), "returncode": 0, "receipt_valid": False})
                continue
            completed.append(run_key(run))
            model_cost += summary["model_cost_usd"]
        if model_cost > plan["cost_budget"]["max_incremental_model_cost_usd"]:
            failures.append(
                {
                    "cost_budget_exceeded": True,
                    "incremental_model_cost_usd": model_cost,
                }
            )
        if failures:
            break
    return {
        "status": "PASS" if not failures else "FAIL",
        "completed": completed,
        "skipped": skipped,
        "failures": failures,
        "incremental_model_cost_usd": model_cost,
        "max_workers": max_workers,
    }


def validate_existing_runs(plan: dict[str, Any], *, runs: list[dict[str, Any]]) -> None:
    existing_sources = plan["source_artifacts"]["existing_runs"]
    for run in runs:
        if not run["existing"]:
            continue
        summary = completed_run_summary(run)
        if summary is None:
            raise ValueError(f"Existing reader replication run is invalid: {run_key(run)}")
        _, current_source = load_replication_source(
            run_dir=Path(run["output_dir"]),
            expected=expected_run_config(run["command"]),
        )
        if current_source != existing_sources[run["configuration"]][run["domain"]]:
            raise ValueError(
                f"Existing reader replication run changed after planning: {run_key(run)}"
            )


def completed_run_summary(run: dict[str, Any]) -> dict[str, Any] | None:
    try:
        summary, _ = load_reader_run(
            run_dir=Path(run["output_dir"]),
            expected=expected_run_config(run["command"]),
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None
    return summary


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
    log_path = output_dir / "replication_runner.log"
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
