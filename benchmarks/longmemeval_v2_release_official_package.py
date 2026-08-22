"""Build one official, score-aware LongMemEval-V2 arm package."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_official_receipt as official_receipt
from benchmarks import longmemeval_v2_release_package_archive as package_archive
from benchmarks import longmemeval_v2_release_package_inventory as package_inventory
from benchmarks import longmemeval_v2_release_package_object as package_object
from benchmarks import longmemeval_v2_release_package_process as process
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_handoff import ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import (
    DOMAINS,
    StagePlanError,
    bind_artifact,
    require_artifact,
)
from benchmarks.longmemeval_v2_release_package_contract import require_package_inputs

SUBMISSION_NAME = official_receipt.SUBMISSION_NAME
OPERATING_POINT = "official"
TIER = official_receipt.TIER
REPO_ROOT = Path(__file__).resolve().parents[1]
ARM_PACKAGE_SCHEMA_VERSION = "sibyl-longmemeval-v2-official-arm-package-v1"
ARM_PACKAGE_KEYS = frozenset(
    {
        "schema_version",
        "stage_plan_sha256",
        "arm_id",
        "status",
        "command_receipts",
        "combined_official_receipt",
        "arm_run",
        "actual_cost_usd",
        "arm_package_sha256",
    }
)
PACKAGE_STEPS = ("operating-point", "submission", "combined-metrics", "official-receipt")
RUN_PACKAGE_FILES = frozenset(
    {
        "aggregated_metrics.json",
        "per_question.jsonl",
        "run_args.json",
        "runtime_inputs/questions.json",
        "runtime_inputs/haystack.json",
        "runtime_inputs/memory_config.json",
    }
)
OPERATING_POINT_FILES = frozenset(
    {
        "metric_overview.json",
        "operating_point_metadata.json",
        *(f"{domain}/{relative}" for domain in DOMAINS for relative in RUN_PACKAGE_FILES),
    }
)


def _require_stage_evidence(executed: ExecutedStage) -> dict[str, Any]:
    if not isinstance(executed, ExecutedStage):
        raise StagePlanError("official packaging requires an executed-stage handoff")
    if executed.status_receipt.get("status") != "EXECUTED":
        raise StagePlanError("official packaging requires an EXECUTED stage")
    runs = state.require_claimed_stage_plan(executed.plan)
    if tuple(runs) != executed.runs:
        raise StagePlanError("executed-stage runs changed before packaging")
    require_package_inputs(executed.plan.get("package_inputs"))
    for name, binding in executed.control_artifacts:
        if name == "runner_status":
            continue
        if require_artifact(binding, name=name) != binding:
            raise StagePlanError("executed-stage control artifact changed")
    for domain in executed.domains:
        if require_artifact(domain.exit_artifact, name="executed domain exit") != (
            domain.exit_artifact
        ):
            raise StagePlanError("executed domain exit changed before packaging")
        for name, binding in domain.artifacts:
            if require_artifact(binding, name=f"executed domain {name}") != binding:
                raise StagePlanError("executed domain artifact changed before packaging")
    status_binding = dict(executed.control_artifacts).get("runner_status")
    if not isinstance(status_binding, dict):
        raise StagePlanError("executed-stage status binding is missing")
    return status_binding


def _require_executed_snapshot(executed: ExecutedStage) -> None:
    status_binding = _require_stage_evidence(executed)
    live_status = state.read_status_receipt(executed.plan)
    if live_status.get("status") != "EXECUTED" or live_status != executed.status_receipt:
        raise StagePlanError("executed-stage status changed before packaging")
    if require_artifact(status_binding, name="executed stage status") != status_binding:
        raise StagePlanError("executed-stage status artifact changed")
    if state.read_status_receipt(executed.plan) != executed.status_receipt:
        raise StagePlanError("executed-stage status changed during packaging validation")


def require_claimed_executed_snapshot(
    executed: ExecutedStage,
    packaging_status: dict[str, Any],
) -> None:
    """Validate paid evidence against an authentic package-lifecycle status."""

    validated = state.validate_status_receipt(executed.plan, packaging_status)
    if validated["status"] not in {"PACKAGING", "PACKAGED"}:
        raise StagePlanError("claimed arm consumption requires package lifecycle status")
    status_binding = _require_stage_evidence(executed)
    if validated["executed_status_artifact"] != status_binding:
        raise StagePlanError("package lifecycle changed the origin EXECUTED binding")
    if state.read_status_receipt(executed.plan) != validated:
        raise StagePlanError("package lifecycle status changed before arm consumption")
    if state.read_status_receipt(executed.plan) != validated:
        raise StagePlanError("package lifecycle status changed during arm consumption")


def _arm_cost(executed: ExecutedStage, run: dict[str, Any]) -> float:
    costs = [
        domain.actual_cost_usd for domain in executed.domains if domain.arm_id == run["arm_id"]
    ]
    if len(costs) != len(DOMAINS):
        raise StagePlanError("executed arm does not contain both Small domains")
    return sum(costs)


def _find_run(executed: ExecutedStage, arm_id: str) -> dict[str, Any]:
    matches = [run for run in executed.runs if run["arm_id"] == arm_id]
    if len(matches) != 1:
        raise StagePlanError("official package arm is not exact in the executed stage")
    return matches[0]


def _validate_package_inputs(
    executed: ExecutedStage,
    parent: package_root.OwnedDirectory,
    arm: package_root.OwnedDirectory,
) -> None:
    _require_executed_snapshot(executed)
    package_root.require_package_roots(parent, arm)


def _step_commands(
    executed: ExecutedStage,
    run: dict[str, Any],
    root: Path,
) -> tuple[dict[str, list[str]], dict[str, Path]]:
    plan = executed.plan
    official_repo = Path(plan["official_source"]["path"])
    submission_root = root / "submission"
    operating_root = submission_root / SUBMISSION_NAME / "operating_points" / OPERATING_POINT
    combined_root = root / "combined"
    combined_metrics = combined_root / "aggregated_metrics.json"
    combined_receipt = combined_root / "longmemeval_v2_official_receipt.json"
    submission_output = Path("submission")
    operating_output = submission_output / SUBMISSION_NAME / "operating_points" / OPERATING_POINT
    combined_output = Path("combined")
    combined_metrics_output = combined_output / "aggregated_metrics.json"
    combined_receipt_output = combined_output / "longmemeval_v2_official_receipt.json"
    web = Path(run["domains"]["web"]["output_dir"])
    enterprise = Path(run["domains"]["enterprise"]["output_dir"])
    package_inputs = plan["package_inputs"]
    receipt_args = [
        "--official-repo",
        str(official_repo),
        "--data-root",
        plan["dataset"]["root"],
        "--domain",
        "combined",
        "--tier",
        TIER,
        "--output-dir",
        str(combined_output),
        "--receipt-only",
        "--metric-overview",
        str(operating_output / "metric_overview.json"),
        "--combined-metrics",
        str(combined_metrics_output),
        "--submission-overview",
        str(submission_output / SUBMISSION_NAME / "submission_overview.json"),
        "--submission-archive",
        str(submission_output / f"{SUBMISSION_NAME}.tar.gz"),
        "--web-output-dir",
        str(web),
        "--enterprise-output-dir",
        str(enterprise),
        "--receipt-output",
        str(combined_receipt_output),
    ]
    commands = {
        "operating-point": [
            sys.executable,
            str(official_repo / "leaderboard/build_submission_step_1_single_operating_point.py"),
            str(web),
            str(enterprise),
            SUBMISSION_NAME,
            OPERATING_POINT,
            TIER,
            "--method",
            SUBMISSION_NAME,
            "--output-root",
            str(submission_output),
        ],
        "submission": [
            sys.executable,
            str(official_repo / "leaderboard/build_submission_step_2_build_package.py"),
            SUBMISSION_NAME,
            package_inputs["system_description"]["path"],
            package_inputs["adapter"]["path"],
            str(operating_output),
            "--output-root",
            str(submission_output),
        ],
        "combined-metrics": [
            sys.executable,
            str(official_repo / "leaderboard/combine_aggregated_metrics.py"),
            str(web / "aggregated_metrics.json"),
            str(enterprise / "aggregated_metrics.json"),
            "-o",
            str(combined_metrics_output),
        ],
        "official-receipt": [
            sys.executable,
            str(REPO_ROOT / "benchmarks/longmemeval_v2_official.py"),
            *receipt_args,
        ],
    }
    paths = {
        "submission_root": submission_root,
        "operating_root": operating_root,
        "combined_root": combined_root,
        "combined_metrics": combined_metrics,
        "combined_receipt": combined_receipt,
    }
    return commands, paths


def _submission_files(adapter_name: str) -> frozenset[str]:
    return frozenset(
        {
            *(f"operating_points/{OPERATING_POINT}/{path}" for path in OPERATING_POINT_FILES),
            "SYSTEM_DESCRIPTION.md",
            adapter_name,
            "submission_overview.json",
        }
    )


def _collectors(
    paths: dict[str, Path],
    *,
    root: Path,
    adapter_name: str,
) -> dict[str, Callable[[], dict[str, Any]]]:
    def operating() -> dict[str, Any]:
        return process.exact_tree_bindings(
            paths["operating_root"],
            package_root=root,
            prefix="operating-point",
            relative_files=OPERATING_POINT_FILES,
        )

    def submission() -> dict[str, Any]:
        bindings = process.exact_tree_bindings(
            paths["submission_root"] / SUBMISSION_NAME,
            package_root=root,
            prefix="submission",
            relative_files=_submission_files(adapter_name),
        )
        archive = paths["submission_root"] / f"{SUBMISSION_NAME}.tar.gz"
        bindings["submission-archive"] = process.owned_binding(
            archive,
            root=root,
            name="official submission archive",
        )
        return bindings

    def combined_metrics() -> dict[str, Any]:
        return {
            "combined-metrics": process.owned_binding(
                paths["combined_metrics"],
                root=root,
                name="official combined metrics",
            )
        }

    def official_receipt() -> dict[str, Any]:
        return process.exact_tree_bindings(
            paths["combined_root"],
            package_root=root,
            prefix="combined",
            relative_files=frozenset(
                {"aggregated_metrics.json", "longmemeval_v2_official_receipt.json"}
            ),
        )

    return {
        "operating-point": operating,
        "submission": submission,
        "combined-metrics": combined_metrics,
        "official-receipt": official_receipt,
    }


def _final_package_files(
    root: Path,
    paths: dict[str, Path],
    *,
    adapter_name: str,
) -> set[Path]:
    files = {
        paths["submission_root"] / SUBMISSION_NAME / relative
        for relative in _submission_files(adapter_name)
    }
    files.update(
        {
            paths["submission_root"] / f"{SUBMISSION_NAME}.tar.gz",
            paths["combined_metrics"],
            paths["combined_receipt"],
            root / "arm_run.json",
            root / "arm_package.json",
        }
    )
    for step in PACKAGE_STEPS:
        files.add(process.log_path(root, step))
        files.add(process.command_receipt_path(root, step))
    return files


def build_official_arm_publication(
    executed: ExecutedStage,
    *,
    arm_id: str,
    packages_root: Path,
) -> None:
    """Build and atomically publish one arm for canonical consumption."""

    _require_executed_snapshot(executed)
    run = _find_run(executed, arm_id)
    parent_owner = package_root.bind_packages_root(
        executed.plan,
        executed.runs,
        packages_root,
    )
    package_root.initialize_packages_root(parent_owner)
    parent_fd = package_root.open_owned_directory(
        parent_owner,
        name="official packages root",
    )
    try:
        package_inventory.require_publication_inventory(
            parent_owner,
            parent_fd,
            arm_ids={run["arm_id"] for run in executed.runs},
        )
    finally:
        os.close(parent_fd)
    lease = package_root.create_arm_lease(parent_owner, arm_id)
    try:
        _build_official_arm_package(executed, run, lease)
        _publish_official_arm_package(executed, run, lease)
    finally:
        lease.close()


def _publish_official_arm_package(
    executed: ExecutedStage,
    run: dict[str, Any],
    lease: package_root.PackageLease,
) -> None:
    """Freeze one validated staging tree into its single public authority."""

    package_root.require_lease(lease)
    files = package_tree.read_owned_tree(lease)
    object_content, manifest = package_archive.build_package_object(files)
    package_binding = package_object.object_binding(
        lease.parent.path,
        run["arm_id"],
        object_content,
    )
    status_binding = dict(executed.control_artifacts).get("runner_status")
    if status_binding is None:
        raise StagePlanError("executed stage status binding is missing")
    arm_run_content = files.get("arm_run.json")
    arm_package_content = files.get("arm_package.json")
    if arm_run_content is None or arm_package_content is None:
        raise StagePlanError("official staging package is incomplete")
    receipt = package_object.build_publication_receipt(
        stage_plan_sha256=executed.plan["stage_plan_sha256"],
        arm_id=run["arm_id"],
        package_object=package_binding,
        package_manifest_sha256=manifest["package_manifest_sha256"],
        executed_status=status_binding,
        arm_run=package_archive.member_binding("arm_run.json", arm_run_content),
        arm_package=package_archive.member_binding("arm_package.json", arm_package_content),
        actual_cost_usd=_arm_cost(executed, run),
    )
    _require_executed_snapshot(executed)
    package_root.require_lease(lease)
    with package_root.publication_lock(lease.parent):
        package_inventory.require_publication_inventory(
            lease.parent,
            lease.parent_fd,
            arm_ids={candidate["arm_id"] for candidate in executed.runs},
        )
        try:
            package_root.thaw_arms_for_publication(lease)
            package_object.publish_arm_authority(
                lease,
                arm_id=run["arm_id"],
                content=object_content,
                receipt=receipt,
            )
        finally:
            package_root.refreeze_arms_after_publication(lease)
        package_inventory.require_publication_inventory(
            lease.parent,
            lease.parent_fd,
            arm_ids={candidate["arm_id"] for candidate in executed.runs},
        )


def _build_official_arm_package(
    executed: ExecutedStage,
    run: dict[str, Any],
    lease: package_root.PackageLease,
) -> None:
    root = lease.arm.path
    commands, paths = _step_commands(executed, run, root)
    collectors = _collectors(
        paths,
        root=root,
        adapter_name=Path(executed.plan["package_inputs"]["adapter"]["path"]).name,
    )
    secrets = state.secret_values(executed.plan)
    receipts: dict[str, process.CompletedCommand] = {}
    for step in PACKAGE_STEPS:
        receipts[step] = process.execute_step(
            root=root,
            step=step,
            command=commands[step],
            collect=collectors[step],
            receipts=receipts,
            secrets=secrets,
            validate_inputs=lambda: _validate_package_inputs(
                executed,
                lease.parent,
                lease.arm,
            ),
            lease=lease,
        )
    combined_before = bind_artifact(
        paths["combined_receipt"],
        name="combined official receipt",
    )
    combined_receipt = official_receipt.require_combined_receipt(
        executed,
        run,
        path=paths["combined_receipt"],
        command=commands["official-receipt"],
        paths=paths,
    )
    arm = official_receipt.build_arm_run(paths["combined_receipt"])
    if (
        bind_artifact(paths["combined_receipt"], name="combined official receipt")
        != combined_before
        or official_receipt.require_combined_receipt(
            executed,
            run,
            path=paths["combined_receipt"],
            command=commands["official-receipt"],
            paths=paths,
        )
        != combined_receipt
    ):
        raise StagePlanError("combined official receipt changed during artifact bridging")
    arm = official_receipt.require_arm_run(
        executed,
        run,
        arm,
        actual_cost_usd=_arm_cost(executed, run),
    )
    arm_path = root / "arm_run.json"
    release_io.write_json_once_atomic_at(lease.arm_fd, "arm_run.json", arm)
    _validate_package_inputs(executed, lease.parent, lease.arm)
    public_receipts = {
        step: bind_artifact(
            process.command_receipt_path(root, step),
            name=f"{step} command receipt",
        )
        for step in PACKAGE_STEPS
    }
    payload = state.sealed(
        {
            "schema_version": ARM_PACKAGE_SCHEMA_VERSION,
            "stage_plan_sha256": executed.plan["stage_plan_sha256"],
            "arm_id": run["arm_id"],
            "status": "PASS",
            "command_receipts": public_receipts,
            "combined_official_receipt": combined_before,
            "arm_run": bind_artifact(arm_path, name="official arm run"),
            "actual_cost_usd": _arm_cost(executed, run),
        },
        "arm_package_sha256",
    )
    release_io.write_json_once_atomic_at(lease.arm_fd, "arm_package.json", payload)
    _require_executed_snapshot(executed)
    process.require_receipts(root, receipts, secrets=secrets)
    process.require_inventory(
        root,
        process.receipt_files(root, receipts) | {root / "arm_run.json", root / "arm_package.json"},
    )
