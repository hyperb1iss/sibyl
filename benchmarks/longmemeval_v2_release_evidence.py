"""Score-blind validation for completed local LongMemEval-V2 domains."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_official as official
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_authorization import reject_score_bearing_keys
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    bind_artifact,
    load_json,
    require_exact_keys,
    require_positive_number,
    require_string,
)
from benchmarks.longmemeval_v2_release_plan import OFFICIAL_COMMAND_PREFIX
from tools.bench import longmemeval_v2_artifact_bridge as bridge

COMPLETION_ARTIFACTS = {
    "official_plan": "longmemeval_v2_official_plan.json",
    "official_receipt": "longmemeval_v2_official_receipt.json",
    "run_args": "run_args.json",
    "aggregated_metrics": "aggregated_metrics.json",
    "per_question": "per_question.jsonl",
    "rig_rows": "rig_rows.jsonl",
    "runtime_questions": "runtime_inputs/questions.json",
    "runtime_haystack": "runtime_inputs/haystack.json",
    "runtime_memory_config": "runtime_inputs/memory_config.json",
    "reader_provider_usage": "provider_usage/reader.jsonl",
    "judge_provider_usage": "provider_usage/judge.jsonl",
}
_RECEIPT_ARTIFACT_NAMES = {
    "official_plan": "plan",
    "run_args": "run_args",
    "aggregated_metrics": "aggregated_metrics",
    "per_question": "per_question",
    "rig_rows": "rig_rows",
    "runtime_questions": "runtime_questions",
    "runtime_haystack": "runtime_haystack",
    "runtime_memory_config": "memory_config",
    "reader_provider_usage": "reader_provider_usage",
    "judge_provider_usage": "judge_provider_usage",
}
_SOURCE_ARTIFACT_NAMES = {
    "official_plan": "plan",
    "run_args": "run_args",
    "aggregated_metrics": "aggregated_metrics",
    "per_question": "per_question",
    "rig_rows": "rig_rows",
}
_RUNTIME_SOURCE_NAMES = {
    "runtime_questions": "questions",
    "runtime_haystack": "haystack",
    "runtime_memory_config": "memory_config",
}


def _option(command: list[str], flag: str) -> str | None:
    if flag not in command:
        return None
    index = command.index(flag)
    if index + 1 >= len(command) or command[index + 1].startswith("--"):
        raise StagePlanError(f"sealed command has no value for {flag}")
    return command[index + 1]


def _require_planning_identity(
    stage_plan: dict[str, Any],
    run: dict[str, Any],
    domain: str,
    raw: dict[str, Any],
) -> None:
    domain_run = run["domains"][domain]
    command = domain_run["plan_command"]
    manifest = run["manifest"]
    expected = {
        "domain": domain,
        "tier": "small",
        "plan_only": True,
        "output_dir": domain_run["planning_output_dir"],
        "data_root": stage_plan["dataset"]["root"],
        "experiment_id": manifest["experiment_id"],
        "experiment_phase": manifest["experiment_phase"],
        "pass_id": run["pass_id"],
        "pass_seed": run["seed"],
        "arm_role": manifest["arm_role"],
        "substrate": manifest["substrate"],
        "preregistration_sha256": manifest["preregistration_sha256"] or None,
        "max_spend_usd": run["spend_reservation"]["max_spend_usd_per_domain"],
        "execution": run["execution"],
        "runner_provenance": stage_plan["sibyl_provenance"],
        "official_source": stage_plan["official_source"],
        "reader_model": stage_plan["spec"]["runtime"]["reader_model"],
        "reader_base_url": stage_plan["spec"]["runtime"]["reader_base_url"],
        "evaluator_model": stage_plan["spec"]["runtime"]["evaluator_model"],
        "save_memory": run["memory_source"].startswith("build_"),
        "load_memory_dir": _option(command, "--load-memory-dir"),
        "checkpoint_dir": _option(command, "--checkpoint-dir"),
    }
    drift = sorted(key for key, value in expected.items() if raw.get(key) != value)
    if drift:
        raise StagePlanError(f"official planning identity drifted: {drift}")


def _require_full_small_selection(
    stage_plan: dict[str, Any], domain: str, raw: dict[str, Any]
) -> None:
    expected_count = stage_plan["dataset"]["question_count_by_domain"][domain]
    expected_ids = stage_plan["dataset"]["question_ids_sha256_by_domain"][domain]
    if (
        raw.get("selection_complete") is not True
        or raw.get("trajectory_path_exists") is not True
        or raw.get("question_count") != expected_count
        or raw.get("official_question_count") != expected_count
        or raw.get("selected_question_ids_sha256") != expected_ids
        or raw.get("official_question_ids_sha256") != expected_ids
    ):
        raise StagePlanError("official planning output does not cover the sealed Small corpus")


def require_planning_output(
    stage_plan: dict[str, Any], run: dict[str, Any], domain: str
) -> dict[str, Any]:
    """Validate a plan-only reservation against its exact sealed command."""

    output_dir = Path(run["domains"][domain]["planning_output_dir"])
    raw = load_json(output_dir / "longmemeval_v2_official_plan.json")
    require_exact_keys(raw, bridge.OFFICIAL_PLAN_KEYS, name="official planning output")
    if raw.get("schema_version") != official.PLAN_SCHEMA_VERSION:
        raise StagePlanError("official planning output schema is invalid")
    _require_planning_identity(stage_plan, run, domain, raw)
    _require_full_small_selection(stage_plan, domain, raw)
    reservation = raw.get("spend_reservation")
    cap = run["spend_reservation"]["max_spend_usd_per_domain"]
    if not isinstance(reservation, dict):
        raise StagePlanError("official planning output has no spend reservation")
    command = run["domains"][domain]["plan_command"]
    args = official.parse_args(command[len(OFFICIAL_COMMAND_PREFIX) :])
    expected_reservation = official.build_spend_reservation(
        args=args,
        question_count=raw["question_count"],
        llm_eval_count=raw["llm_eval_count"],
        required_trajectory_count=raw["required_trajectory_count"],
    )
    if reservation != expected_reservation:
        raise StagePlanError("official planning reservation differs from its sealed command")
    official.enforce_spend_reservation(raw)
    reserved = require_positive_number(
        reservation.get("reserved_total_usd"), name="official reserved spend"
    )
    if reservation.get("max_spend_usd") != cap or reserved > cap:
        raise StagePlanError("official planning output exceeds its sealed reservation")
    requirements = raw.get("requirements")
    if (
        not isinstance(requirements, dict)
        or not requirements
        or not all(value is True for value in requirements.values())
    ):
        raise StagePlanError("official planning requirements are not all satisfied")
    return bind_artifact(
        output_dir / "longmemeval_v2_official_plan.json",
        name="official planning output",
    )


def _normalized_preregistration(manifest: dict[str, Any]) -> str:
    value = str(manifest["preregistration_sha256"])
    return f"sha256:{value}" if value else ""


def _require_plan_identity(
    stage_plan: dict[str, Any],
    run: dict[str, Any],
    domain: str,
    plan: dict[str, Any],
) -> None:
    domain_run = run["domains"][domain]
    command = domain_run["run_command"]
    manifest = run["manifest"]
    runtime = stage_plan["spec"]["runtime"]
    expected = {
        "domain": domain,
        "tier": "small",
        "plan_only": False,
        "output_dir": domain_run["output_dir"],
        "data_root": stage_plan["dataset"]["root"],
        "experiment_id": manifest["experiment_id"],
        "experiment_phase": manifest["experiment_phase"],
        "pass_id": run["pass_id"],
        "pass_seed": run["seed"],
        "arm_role": manifest["arm_role"],
        "substrate": manifest["substrate"],
        "preregistration_sha256": _normalized_preregistration(manifest),
        "max_spend_usd": run["spend_reservation"]["max_spend_usd_per_domain"],
        "execution": run["execution"],
        "runner_provenance": stage_plan["sibyl_provenance"],
        "official_source": stage_plan["official_source"],
        "reader_model": runtime["reader_model"],
        "reader_base_url": runtime["reader_base_url"],
        "reader_max_concurrent_requests": runtime["reader_max_concurrent_requests"],
        "reader_retry_attempts": runtime["reader_retry_attempts"],
        "evaluator_model": runtime["evaluator_model"],
        "evidence_composition_mode": runtime["evidence_composition_mode"],
        "retrieval_max_planned_queries": runtime["retrieval_max_planned_queries"],
        "typed_stream_limit": runtime["typed_stream_limit"],
        "note_distillation_model": runtime["note_distillation_model"],
        "memory_api_retry_attempts": runtime["api_retry_attempts"],
        "save_memory": run["memory_source"].startswith("build_"),
        "load_memory_dir": _option(command, "--load-memory-dir"),
        "checkpoint_dir": _option(command, "--checkpoint-dir"),
    }
    drift = sorted(key for key, value in expected.items() if plan.get(key) != value)
    if drift:
        raise StagePlanError(f"official completed plan identity drifted: {drift}")


def _safe_artifact(output_dir: Path, relative: str, *, name: str) -> dict[str, Any]:
    candidate = output_dir / relative
    if candidate.is_symlink() or candidate.resolve() != candidate:
        raise StagePlanError(f"{name} escapes its official output through a symlink")
    return bind_artifact(candidate, name=name)


def _artifact_bindings(output_dir: Path) -> dict[str, Any]:
    return {
        name: _safe_artifact(output_dir, relative, name=f"completed domain {name}")
        for name, relative in COMPLETION_ARTIFACTS.items()
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise StagePlanError(f"JSONL row is not an object: {path}")
                    rows.append(row)
    except (OSError, json.JSONDecodeError) as exc:
        raise StagePlanError(f"could not load completed JSONL artifact: {path}") from exc
    return rows


def _require_receipt_artifacts(
    receipt: dict[str, Any],
    artifacts: dict[str, Any],
) -> None:
    raw = receipt.get("artifacts")
    if not isinstance(raw, dict):
        raise StagePlanError("official receipt artifact bindings are missing")
    for local_name, receipt_name in _RECEIPT_ARTIFACT_NAMES.items():
        bridge._artifact_path(  # pyright: ignore[reportPrivateUsage]
            raw.get(receipt_name),
            expected_path=Path(artifacts[local_name]["path"]),
            name=f"official receipt {receipt_name}",
        )


def _source_domain(receipt: dict[str, Any], *, domain: str) -> dict[str, Any]:
    source_runs = receipt.get("source_runs")
    if not isinstance(source_runs, dict):
        raise StagePlanError("official receipt source runs are missing")
    require_exact_keys(source_runs, bridge.SOURCE_RUNS_KEYS, name="official source runs")
    passing_flags = (
        "complete",
        "integrity_complete",
        "api_runtime_consistent",
        "model_consistent",
        "method_consistent",
    )
    if source_runs.get("expected_domains") != [domain] or any(
        source_runs.get(flag) is not True for flag in passing_flags
    ):
        raise StagePlanError("official source-run summary is incomplete")
    domains = source_runs.get("domains")
    if not isinstance(domains, dict) or set(domains) != {domain}:
        raise StagePlanError("official source-run domains are not exact")
    source = domains[domain]
    if not isinstance(source, dict):
        raise StagePlanError("official domain source record is missing")
    require_exact_keys(source, bridge.DOMAIN_SOURCE_KEYS, name="official domain source")
    return source


def _require_source_artifacts(
    source: dict[str, Any],
    *,
    domain: str,
    output_dir: Path,
    artifacts: dict[str, Any],
) -> None:
    if Path(str(source.get("output_dir"))).resolve() != output_dir:
        raise StagePlanError("official source output directory drifted")
    for local_name, source_name in _SOURCE_ARTIFACT_NAMES.items():
        bridge._artifact_path(  # pyright: ignore[reportPrivateUsage]
            source.get(source_name),
            expected_path=Path(artifacts[local_name]["path"]),
            name=f"official source {source_name}",
        )
    runtime_inputs = source.get("runtime_inputs")
    if not isinstance(runtime_inputs, dict):
        raise StagePlanError("official source runtime inputs are missing")
    require_exact_keys(runtime_inputs, bridge.RUNTIME_INPUT_KEYS, name="source runtime inputs")
    for local_name, source_name in _RUNTIME_SOURCE_NAMES.items():
        bridge._artifact_path(  # pyright: ignore[reportPrivateUsage]
            runtime_inputs.get(source_name),
            expected_path=Path(artifacts[local_name]["path"]),
            name=f"official source runtime {source_name}",
        )


def _require_source_runtime(
    source: dict[str, Any],
    *,
    domain: str,
    artifacts: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    provider_usage = source.get("provider_usage")
    if not isinstance(provider_usage, dict):
        raise StagePlanError("official source provider usage is missing")
    require_exact_keys(provider_usage, bridge.PROVIDER_ROLE_KEYS, name="source provider usage")
    for role in sorted(bridge.PROVIDER_ROLE_KEYS):
        bridge._provider_record(  # pyright: ignore[reportPrivateUsage]
            provider_usage.get(role),
            expected_path=Path(artifacts[f"{role}_provider_usage"]["path"]),
            expected_run_id=str(plan["provider_usage_run_id"]),
            name=f"{domain}.{role} provider usage",
        )
    bridge._validate_runtime(  # pyright: ignore[reportPrivateUsage]
        source.get("api_runtime"),
        execution_sha=str(plan["execution"]["sha"]),
        name=domain,
    )
    if source.get("api_runtime_consistent") is not True:
        raise StagePlanError("official source API runtime is inconsistent")
    return bridge._configuration_and_geometry(  # pyright: ignore[reportPrivateUsage]
        source.get("effective_memory_config"),
        plan=plan,
        domain=domain,
    )


def _require_source_record(
    receipt: dict[str, Any],
    *,
    domain: str,
    output_dir: Path,
    artifacts: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    source = _source_domain(receipt, domain=domain)
    _require_source_artifacts(
        source,
        domain=domain,
        output_dir=output_dir,
        artifacts=artifacts,
    )
    configuration, geometry = _require_source_runtime(
        source,
        domain=domain,
        artifacts=artifacts,
        plan=plan,
    )
    for field in ("reader_model", "reader_base_url", "evaluator_model", "method", "tier"):
        expected = plan[field]
        if source.get(field) != expected:
            raise StagePlanError(f"official source differs from its plan for {field}")
    return source, configuration, geometry


def _require_run_args(
    artifacts: dict[str, Any],
    *,
    domain: str,
    output_dir: Path,
    plan: dict[str, Any],
) -> None:
    raw = load_json(Path(artifacts["run_args"]["path"]))
    if not bridge.RUN_ARGS_REQUIRED_KEYS.issubset(raw):
        raise StagePlanError("official run args omit canonical required keys")
    expected = {
        "domain": domain,
        "questions_path": artifacts["runtime_questions"]["path"],
        "haystack_path": artifacts["runtime_haystack"]["path"],
        "memory_config_path": artifacts["runtime_memory_config"]["path"],
        "output_dir": str(output_dir),
        "model": plan["reader_model"],
        "base_url": plan["reader_base_url"],
        "evaluator_model": plan["evaluator_model"],
        "shuffle_questions_seed": plan["pass_seed"],
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise StagePlanError("official run args differ from the sealed completed plan")


def _require_rig_rows(artifacts: dict[str, Any]) -> None:
    rig_rows = _load_jsonl(Path(artifacts["rig_rows"]["path"]))
    reject_score_bearing_keys(rig_rows, name="completed rig rows")
    questions = json.loads(Path(artifacts["runtime_questions"]["path"]).read_text(encoding="utf-8"))
    if not isinstance(questions, list):
        raise StagePlanError("completed runtime questions are invalid")
    question_ids = [
        require_string(item.get("id"), name="runtime question id") for item in questions
    ]
    rig_ids: list[str] = []
    for index, row in enumerate(rig_rows):
        require_exact_keys(row, bridge.RIG_ROW_KEYS, name=f"rig_rows[{index}]")
        if row.get("status") != "valid":
            raise StagePlanError("completed rig row is not valid")
        rig_ids.append(require_string(row.get("question_id"), name="rig row question id"))
    if len(set(rig_ids)) != len(rig_ids) or set(rig_ids) != set(question_ids):
        raise StagePlanError("completed rig rows differ from the official question set")


def require_completed_domain(
    stage_plan: dict[str, Any],
    run: dict[str, Any],
    domain: str,
) -> tuple[float, dict[str, Any]]:
    """Validate score-blind official evidence and return its cost and bindings."""

    output_dir = Path(run["domains"][domain]["output_dir"])
    if output_dir.is_symlink() or output_dir.resolve() != output_dir or not output_dir.is_dir():
        raise StagePlanError("completed domain output is not one canonical directory")
    state.require_domain_output_tree(run, domain)
    artifacts = _artifact_bindings(output_dir)
    plan = bridge._validate_plan(  # pyright: ignore[reportPrivateUsage]
        load_json(Path(artifacts["official_plan"]["path"])),
        domain=domain,
    )
    _require_plan_identity(stage_plan, run, domain, plan)
    receipt = bridge._validate_receipt(  # pyright: ignore[reportPrivateUsage]
        load_json(Path(artifacts["official_receipt"]["path"])),
        name=f"{domain} official receipt",
        domain=domain,
    )
    expected_command = [
        "benchmarks/longmemeval_v2_official.py",
        *official._redacted_command_args(  # pyright: ignore[reportPrivateUsage]
            run["domains"][domain]["run_command"][len(OFFICIAL_COMMAND_PREFIX) :]
        ),
    ]
    if (
        receipt.get("command") != expected_command
        or receipt.get("sibyl_commit") != stage_plan["source_identity"]["sha"]
        or receipt.get("runner_provenance") != stage_plan["sibyl_provenance"]
        or receipt.get("official_repo") != stage_plan["official_source"]
        or receipt.get("method") != plan["method"]
    ):
        raise StagePlanError("official receipt differs from the sealed command or source")
    dataset_hashes = bridge._validate_dataset_and_models(  # pyright: ignore[reportPrivateUsage]
        receipt,
        plan=plan,
        domain=domain,
    )
    expected_hashes = {
        "questions_sha256": stage_plan["dataset"]["artifacts"]["questions"]["sha256"],
        "trajectories_sha256": stage_plan["dataset"]["artifacts"]["trajectories"]["sha256"],
        "haystack_sha256": stage_plan["dataset"]["artifacts"]["small_haystack"]["sha256"],
    }
    if dataset_hashes != expected_hashes:
        raise StagePlanError("official receipt dataset differs from the sealed release corpus")
    _require_receipt_artifacts(receipt, artifacts)
    _source, configuration, geometry = _require_source_record(
        receipt,
        domain=domain,
        output_dir=output_dir,
        artifacts=artifacts,
        plan=plan,
    )
    manifest = run["manifest"]
    if any(configuration.get(key) != value for key, value in manifest["configuration"].items()):
        raise StagePlanError("effective memory configuration differs from the sealed arm")
    if geometry != manifest["geometry"]:
        raise StagePlanError("effective memory geometry differs from the sealed arm")
    _require_run_args(artifacts, domain=domain, output_dir=output_dir, plan=plan)
    _require_rig_rows(artifacts)
    actual_cost, _tokens = bridge._domain_cost_and_tokens(  # pyright: ignore[reportPrivateUsage]
        receipt,
        domain=domain,
    )
    cap = run["spend_reservation"]["max_spend_usd_per_domain"]
    if actual_cost > cap:
        raise StagePlanError("completed domain exceeds its sealed spend reservation")
    return actual_cost, artifacts
