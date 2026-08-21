"""Validate score-aware official receipts at the release package boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_official as official
from benchmarks.longmemeval_v2_release_handoff import ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import (
    DOMAINS,
    StagePlanError,
    load_json,
    require_artifact,
)
from tools.bench import eval_gate
from tools.bench import longmemeval_v2_artifact_bridge as bridge
from tools.bench import longmemeval_v2_rig as rig

SUBMISSION_NAME = "sibyl_live_api"
TIER = "small"


def _require_official_artifact(raw: object, *, expected: Path, name: str) -> None:
    if not isinstance(raw, dict) or set(raw) != bridge.ARTIFACT_RECORD_KEYS:
        raise StagePlanError(f"combined receipt {name} artifact is invalid")
    binding = {
        "path": raw.get("path"),
        "sha256": raw.get("sha256"),
        "size_bytes": raw.get("size_bytes"),
    }
    if raw.get("exists") is not True or require_artifact(binding, name=name)["path"] != str(
        expected
    ):
        raise StagePlanError(f"combined receipt {name} artifact path is invalid")


def require_combined_receipt(
    executed: ExecutedStage,
    run: dict[str, Any],
    *,
    path: Path,
    command: list[str],
    paths: dict[str, Path],
) -> dict[str, Any]:
    """Validate the gated combined receipt against the exact sealed run."""

    receipt = load_json(path)
    failures = eval_gate.evaluate_report(receipt, profile="longmemeval-v2")
    if failures:
        raise StagePlanError(f"combined official receipt failed its release gate: {failures}")
    receipt_args = command[2:]
    plan = executed.plan
    expected_hashes = {
        "questions_sha256": plan["dataset"]["artifacts"]["questions"]["sha256"],
        "trajectories_sha256": plan["dataset"]["artifacts"]["trajectories"]["sha256"],
        "haystack_sha256": plan["dataset"]["artifacts"]["small_haystack"]["sha256"],
    }
    questions = official.load_longmemeval_v2_questions(
        Path(plan["dataset"]["artifacts"]["questions"]["path"])
    )
    question_ids = [question.id for question in questions if question.domain in DOMAINS]
    question_count, required_trajectory_count = official.summarize_dataset_counts(
        data_root=Path(plan["dataset"]["root"]),
        domain="combined",
        tier=TIER,
    )
    question_ids_sha256 = official.sha256_question_ids(question_ids)
    expected_dataset = {
        "name": "longmemeval-v2",
        "data_root": plan["dataset"]["root"],
        "tier": TIER,
        **expected_hashes,
        "question_count": question_count,
        "selected_question_ids_sha256": question_ids_sha256,
        "official_question_count": question_count,
        "official_question_ids_sha256": question_ids_sha256,
        "selection_complete": True,
        "required_trajectory_count": required_trajectory_count,
    }
    if (
        receipt.get("command") != ["benchmarks/longmemeval_v2_official.py", *receipt_args]
        or receipt.get("domain") != "combined"
        or receipt.get("tier") != TIER
        or receipt.get("method") != SUBMISSION_NAME
        or receipt.get("sibyl_commit") != plan["source_identity"]["sha"]
        or receipt.get("runner_provenance") != plan["sibyl_provenance"]
        or receipt.get("official_repo") != plan["official_source"]
        or receipt.get("dataset") != expected_dataset
        or question_count != sum(plan["dataset"]["question_count_by_domain"].values())
    ):
        raise StagePlanError("combined official receipt differs from the sealed release identity")
    runtime = plan["spec"]["runtime"]
    models = receipt.get("models")
    if not isinstance(models, dict) or any(
        models.get(key) != value
        for key, value in {
            "reader_model": runtime["reader_model"],
            "reader_base_url": runtime["reader_base_url"],
            "evaluator_model": runtime["evaluator_model"],
        }.items()
    ):
        raise StagePlanError("combined official receipt model pins changed")
    source_runs = receipt.get("source_runs")
    if not isinstance(source_runs, dict) or not isinstance(source_runs.get("domains"), dict):
        raise StagePlanError("combined official receipt source runs are missing")
    for domain in DOMAINS:
        source = source_runs["domains"].get(domain)
        if (
            not isinstance(source, dict)
            or source.get("output_dir") != run["domains"][domain]["output_dir"]
        ):
            raise StagePlanError("combined official receipt source output changed")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        raise StagePlanError("combined official receipt artifacts are missing")
    for name, expected in {
        "metric_overview": paths["operating_root"] / "metric_overview.json",
        "combined_metrics": paths["combined_metrics"],
        "submission_overview": paths["submission_root"]
        / SUBMISSION_NAME
        / "submission_overview.json",
        "submission_archive": paths["submission_root"] / f"{SUBMISSION_NAME}.tar.gz",
    }.items():
        _require_official_artifact(artifacts.get(name), expected=expected, name=name)
    return receipt


def require_arm_run(
    executed: ExecutedStage,
    run: dict[str, Any],
    arm: dict[str, Any],
    *,
    actual_cost_usd: float,
) -> dict[str, Any]:
    """Validate one bridged arm against its sealed role and executed cost."""

    manifest = run["manifest"]
    try:
        validated = rig.validate_arm(
            arm,
            stack_digest=rig.stack_fingerprint(executed.plan["stack_identity"]),
            side=manifest["arm_role"],
        )
    except rig.RigInputError as exc:
        raise StagePlanError("official bridge produced an invalid arm run") from exc
    preregistration = manifest["preregistration_sha256"]
    expected = {
        "experiment_id": manifest["experiment_id"],
        "experiment_phase": manifest["experiment_phase"],
        "pass_id": run["pass_id"],
        "seed": run["seed"],
        "name": manifest["arm_role"],
        "substrate": manifest["substrate"],
        "preregistration_sha256": (f"sha256:{preregistration}" if preregistration else ""),
        "execution": run["execution"],
        "stack": executed.plan["stack_identity"],
        "configuration": manifest["configuration"],
        "geometry": manifest["geometry"],
    }
    if any(validated.get(key) != value for key, value in expected.items()):
        raise StagePlanError("official arm run differs from its sealed stage arm")
    if validated["provider_usage"]["actual_cost_usd"] != actual_cost_usd:
        raise StagePlanError("official arm run cost differs from executed domain receipts")
    return validated


def build_arm_run(path: Path) -> dict[str, Any]:
    """Build one canonical bridge arm and normalize its failure boundary."""

    try:
        return bridge.build_arm_run(path)
    except (OSError, ValueError) as exc:
        raise StagePlanError("official artifact bridge rejected the packaged arm") from exc
