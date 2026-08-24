"""CI orchestration and portable handoff for the v1.3 A/A release stage."""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_authorization_package as authorization_package
from benchmarks import longmemeval_v2_release_contract as contract
from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    load_json,
    require_exact_keys,
    require_source_identity,
    require_string,
    sha256_file,
)
from tools.bench import longmemeval_v2_artifact_bridge as bridge
from tools.bench import longmemeval_v2_rig as rig

DISPATCH_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-ci-dispatch-plan-v1"
RUN_MAP_SCHEMA_VERSION = "sibyl-longmemeval-v2-ci-run-map-v1"
BUNDLE_MANIFEST_SCHEMA_VERSION = "sibyl-longmemeval-v2-ci-aa-bundle-v1"
PASS_SEEDS = {"aa-1": 1301, "aa-2": 1302, "aa-3": 1303}
ARM_IDS = tuple(f"{pass_id}-{side}" for pass_id in PASS_SEEDS for side in ("left", "right"))
BUILDER_ARM_ID = "aa-1-left"
RUN_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
ORCHESTRATION_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


def _runtime() -> dict[str, Any]:
    return {
        "api_url": "http://127.0.0.1:3334/api",
        "allow_localhost": True,
        **contract.RELEASE_RUNTIME_PINS,
    }


def _manifest(*, experiment_id: str, pass_id: str, seed: int) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "experiment_phase": "aa",
        "pass_id": pass_id,
        "pass_seed": seed,
        "arm_role": "machine",
        "substrate": "machine",
        "preregistration_sha256": "",
        "max_spend_usd": contract.RELEASE_ROLE_CAPS_USD["machine"],
        "retrieval_mode": "fast",
        "max_context_total_chars": contract.BASE_CONTEXT_TOTAL_CHARS,
        "operational_note_dedupe_mode": "source",
        "operational_note_lane_mode": "reserved",
        "operational_note_distillation_profile": "baseline",
        "render_group_lanes": False,
        "render_action_spines": False,
        "configuration": {
            "retrieval_mode": "fast",
            "max_context_chars_per_item": contract.RELEASE_RUNTIME_PINS[
                "max_context_chars_per_item"
            ],
            "operational_note_dedupe_mode": "source",
            "operational_note_lane_mode": "reserved",
            "operational_note_distillation_profile": "baseline",
            "render_group_lanes": False,
            "render_action_spines": False,
        },
        "geometry": {
            "max_context_items": contract.MAX_CONTEXT_ITEMS,
            "max_context_chars_per_item": contract.RELEASE_RUNTIME_PINS[
                "max_context_chars_per_item"
            ],
            "max_context_total_chars": contract.BASE_CONTEXT_TOTAL_CHARS,
        },
    }


def build_aa_stage_spec(experiment_id: str) -> dict[str, Any]:
    """Build and validate the immutable initial A/A schedule used by CI."""

    passes: list[dict[str, Any]] = []
    for pass_id, seed in PASS_SEEDS.items():
        arms = []
        for side in ("left", "right"):
            arm_id = f"{pass_id}-{side}"
            arms.append(
                {
                    "arm_id": arm_id,
                    "memory_source": ("build_baseline" if arm_id == BUILDER_ARM_ID else "baseline"),
                    "manifest": _manifest(
                        experiment_id=experiment_id,
                        pass_id=pass_id,
                        seed=seed,
                    ),
                }
            )
        passes.append({"kind": "paired", "pass_id": pass_id, "seed": seed, "arms": arms})
    spec = {
        "schema_version": contract.STAGE_SPEC_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "stage": "aa",
        "mode": "initial",
        "runtime": _runtime(),
        "memory_roots": {"baseline": None, "render": None},
        "upstream": {
            "aa_authorization": None,
            "preregistration_authorization": None,
        },
        "passes": passes,
    }
    return contract.require_stage_spec(spec)


def _official_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    projected = deepcopy(manifest)
    projected.pop("configuration")
    projected.pop("geometry")
    return projected


def build_dispatch_plan(
    *,
    experiment_id: str,
    orchestration_id: str,
    source: dict[str, str],
) -> dict[str, Any]:
    """Return the exact six child workflow dispatches for initial A/A."""

    if not ORCHESTRATION_ID_PATTERN.fullmatch(orchestration_id):
        raise StagePlanError("CI orchestration ID is not path-safe")
    validated_source = require_source_identity(source)
    spec = build_aa_stage_spec(experiment_id)
    arms = []
    for pass_spec in spec["passes"]:
        for arm in pass_spec["arms"]:
            arm_id = str(arm["arm_id"])
            arms.append(
                {
                    "arm_id": arm_id,
                    "pass_id": pass_spec["pass_id"],
                    "seed": pass_spec["seed"],
                    "side": arm_id.rsplit("-", 1)[1],
                    "memory_mode": "save" if arm_id == BUILDER_ARM_ID else "load",
                    "official_arm_manifest_json": json.dumps(
                        _official_manifest(arm["manifest"]),
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
            )
    payload = {
        "schema_version": DISPATCH_PLAN_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "orchestration_id": orchestration_id,
        "source": validated_source,
        "builder_arm_id": BUILDER_ARM_ID,
        "arms": arms,
    }
    payload["dispatch_plan_sha256"] = rig.canonical_sha256(payload)
    return payload


def require_run_map(raw: object) -> dict[str, Any]:
    """Validate the controller's immutable mapping from arms to workflow runs."""

    if not isinstance(raw, dict):
        raise StagePlanError("CI run map is missing")
    require_exact_keys(
        raw,
        frozenset(
            {
                "schema_version",
                "experiment_id",
                "orchestration_id",
                "source",
                "builder_run_id",
                "runs",
                "run_map_sha256",
            }
        ),
        name="CI run map",
    )
    unsigned = {key: value for key, value in raw.items() if key != "run_map_sha256"}
    if raw.get("schema_version") != RUN_MAP_SCHEMA_VERSION or raw.get(
        "run_map_sha256"
    ) != rig.canonical_sha256(unsigned):
        raise StagePlanError("CI run map schema or digest is invalid")
    require_string(raw.get("experiment_id"), name="CI run map experiment ID")
    orchestration_id = require_string(
        raw.get("orchestration_id"), name="CI run map orchestration ID"
    )
    if not ORCHESTRATION_ID_PATTERN.fullmatch(orchestration_id):
        raise StagePlanError("CI run map orchestration ID is not path-safe")
    source = require_source_identity(raw.get("source"))
    runs = raw.get("runs")
    if not isinstance(runs, dict) or tuple(runs) != ARM_IDS:
        raise StagePlanError("CI run map arm order or membership is invalid")
    if any(
        not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id)
        for run_id in runs.values()
    ):
        raise StagePlanError("CI run map contains a non-canonical workflow run ID")
    if len(set(runs.values())) != len(ARM_IDS):
        raise StagePlanError("CI run map reused a workflow run ID")
    if raw.get("builder_run_id") != runs[BUILDER_ARM_ID]:
        raise StagePlanError("CI run map builder identity is inconsistent")
    return {**raw, "source": source, "runs": dict(runs)}


def build_run_map(
    *,
    dispatch_plan: dict[str, Any],
    runs: dict[str, str],
) -> dict[str, Any]:
    """Bind every planned arm to the distinct workflow run that executed it."""

    if dispatch_plan.get("schema_version") != DISPATCH_PLAN_SCHEMA_VERSION:
        raise StagePlanError("CI dispatch plan schema is invalid")
    unsigned_plan = {
        key: value for key, value in dispatch_plan.items() if key != "dispatch_plan_sha256"
    }
    if dispatch_plan.get("dispatch_plan_sha256") != rig.canonical_sha256(unsigned_plan):
        raise StagePlanError("CI dispatch plan digest is invalid")
    planned_arms = dispatch_plan.get("arms")
    if not isinstance(planned_arms, list) or [
        arm.get("arm_id") for arm in planned_arms if isinstance(arm, dict)
    ] != list(ARM_IDS):
        raise StagePlanError("CI dispatch plan arm order or membership is invalid")
    payload = {
        "schema_version": RUN_MAP_SCHEMA_VERSION,
        "experiment_id": dispatch_plan["experiment_id"],
        "orchestration_id": dispatch_plan["orchestration_id"],
        "source": dispatch_plan["source"],
        "builder_run_id": runs.get(BUILDER_ARM_ID),
        "runs": runs,
    }
    payload["run_map_sha256"] = rig.canonical_sha256(payload)
    return require_run_map(payload)


def _find_arm_run(root: Path, arm_id: str) -> Path:
    arm_root = (root / arm_id).resolve()
    if not arm_root.is_dir() or not arm_root.is_relative_to(root.resolve()):
        raise StagePlanError(f"CI arm artifact root is missing: {arm_id}")
    matches = sorted(arm_root.rglob("arm_run.json"))
    if len(matches) != 1:
        raise StagePlanError(f"CI arm artifact count is not exact: {arm_id}")
    return matches[0]


def aggregate_aa_bundle(
    *,
    artifacts_root: Path,
    run_map_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Validate six child arms and publish one portable A/A evidence bundle."""

    run_map = require_run_map(load_json(run_map_path))
    artifacts = artifacts_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise StagePlanError("CI A/A bundle output already exists")
    arm_runs: dict[str, dict[str, Any]] = {}
    for arm_id, expected_run_id in run_map["runs"].items():
        arm = load_json(_find_arm_run(artifacts, arm_id))
        execution = arm.get("execution")
        if not isinstance(execution, dict):
            raise StagePlanError(f"CI arm execution is missing: {arm_id}")
        expected_identity = {
            **run_map["source"],
            "run_id": expected_run_id,
        }
        if any(execution.get(key) != value for key, value in expected_identity.items()):
            raise StagePlanError(f"CI arm execution differs from its run map: {arm_id}")
        if arm.get("experiment_id") != run_map["experiment_id"]:
            raise StagePlanError(f"CI arm experiment differs from its run map: {arm_id}")
        pass_id, side = arm_id.rsplit("-", 1)
        expected_arm = {
            "experiment_phase": "aa",
            "pass_id": pass_id,
            "seed": PASS_SEEDS[pass_id],
            "name": "machine",
            "substrate": "machine",
            "preregistration_sha256": "",
        }
        if any(arm.get(key) != value for key, value in expected_arm.items()):
            raise StagePlanError(f"CI arm differs from the fixed A/A schedule: {arm_id}")
        if side not in {"left", "right"}:
            raise StagePlanError(f"CI arm side is invalid: {arm_id}")
        arm_runs[arm_id] = arm

    paired: list[dict[str, Any]] = []
    for pass_id in PASS_SEEDS:
        paired.append(
            bridge.build_paired_pass(
                arm_runs[f"{pass_id}-left"],
                arm_runs[f"{pass_id}-right"],
            )
        )
    receipt = rig.build_aa_receipt(paired)

    output.mkdir(parents=True)
    passes_root = output / "passes"
    passes_root.mkdir()
    receipt_path = output / "aa_receipt.json"
    release_io.write_json_once_atomic(receipt_path, receipt)
    pass_bindings = []
    for pass_id, payload in zip(PASS_SEEDS, paired, strict=True):
        path = passes_root / f"{pass_id}.json"
        release_io.write_json_once_atomic(path, payload)
        pass_bindings.append(
            {
                "pass_id": pass_id,
                "path": f"passes/{pass_id}.json",
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    release_io.write_json_once_atomic(output / "run_map.json", run_map)
    manifest = {
        "schema_version": BUNDLE_MANIFEST_SCHEMA_VERSION,
        "experiment_id": run_map["experiment_id"],
        "orchestration_id": run_map["orchestration_id"],
        "source": run_map["source"],
        "status": receipt["status"],
        "aa_receipt": {
            "path": "aa_receipt.json",
            "sha256": sha256_file(receipt_path),
            "size_bytes": receipt_path.stat().st_size,
        },
        "passes": pass_bindings,
        "run_map": {
            "path": "run_map.json",
            "sha256": sha256_file(output / "run_map.json"),
            "size_bytes": (output / "run_map.json").stat().st_size,
        },
    }
    manifest["bundle_manifest_sha256"] = rig.canonical_sha256(manifest)
    release_io.write_json_once_atomic(output / "bundle_manifest.json", manifest)
    return manifest


def _bound_bundle_file(root: Path, raw: object, *, name: str) -> Path:
    if not isinstance(raw, dict):
        raise StagePlanError(f"{name} binding is missing")
    require_exact_keys(raw, frozenset({"path", "sha256", "size_bytes"}), name=name)
    relative = Path(require_string(raw.get("path"), name=f"{name}.path"))
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root):
        raise StagePlanError(f"{name} escapes the CI bundle")
    if (
        not path.is_file()
        or path.stat().st_size != raw.get("size_bytes")
        or sha256_file(path) != raw.get("sha256")
    ):
        raise StagePlanError(f"{name} bytes differ from the CI bundle manifest")
    return path


def import_aa_bundle(*, bundle_root: Path, output: Path) -> dict[str, Any]:
    """Rebind a downloaded CI bundle to canonical local artifact paths."""

    root = bundle_root.expanduser().resolve()
    manifest = load_json(root / "bundle_manifest.json")
    require_exact_keys(
        manifest,
        frozenset(
            {
                "schema_version",
                "experiment_id",
                "orchestration_id",
                "source",
                "status",
                "aa_receipt",
                "passes",
                "run_map",
                "bundle_manifest_sha256",
            }
        ),
        name="CI A/A bundle manifest",
    )
    unsigned = {key: value for key, value in manifest.items() if key != "bundle_manifest_sha256"}
    if manifest.get("schema_version") != BUNDLE_MANIFEST_SCHEMA_VERSION or manifest.get(
        "bundle_manifest_sha256"
    ) != rig.canonical_sha256(unsigned):
        raise StagePlanError("CI A/A bundle manifest schema or digest is invalid")
    receipt_path = _bound_bundle_file(root, manifest.get("aa_receipt"), name="A/A receipt")
    _bound_bundle_file(root, manifest.get("run_map"), name="CI run map")
    raw_passes = manifest.get("passes")
    if not isinstance(raw_passes, list) or [
        item.get("pass_id") for item in raw_passes if isinstance(item, dict)
    ] != list(PASS_SEEDS):
        raise StagePlanError("CI A/A bundle pass order or membership is invalid")
    pass_paths = []
    for item in raw_passes:
        assert isinstance(item, dict)
        binding = {key: item.get(key) for key in ("path", "sha256", "size_bytes")}
        pass_paths.append(
            _bound_bundle_file(
                root,
                binding,
                name=f"A/A paired pass {item['pass_id']}",
            )
        )
    payload = authorization_package.package_aa_authorization(
        receipt_path,
        paired_pass_paths=pass_paths,
    )
    authorization_package.write_authorization(
        output.expanduser().resolve(),
        payload,
        paid_output_root=root,
    )
    return payload


def _write_stdout(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--experiment-id", required=True)
    plan.add_argument("--orchestration-id", required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--ref", required=True)
    plan.add_argument("--sha", required=True)
    plan.add_argument("--output", required=True)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--artifacts-root", required=True)
    aggregate.add_argument("--run-map", required=True)
    aggregate.add_argument("--output-root", required=True)
    run_map = commands.add_parser("run-map")
    run_map.add_argument("--dispatch-plan", required=True)
    run_map.add_argument("--runs", required=True)
    run_map.add_argument("--output", required=True)
    import_aa = commands.add_parser("import-aa")
    import_aa.add_argument("--bundle-root", required=True)
    import_aa.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "plan":
        payload = build_dispatch_plan(
            experiment_id=args.experiment_id,
            orchestration_id=args.orchestration_id,
            source={"repository": args.repository, "ref": args.ref, "sha": args.sha},
        )
        release_io.write_json_once_atomic(Path(args.output).resolve(), payload)
    elif args.command == "run-map":
        payload = build_run_map(
            dispatch_plan=load_json(Path(args.dispatch_plan)),
            runs=load_json(Path(args.runs)),
        )
        release_io.write_json_once_atomic(Path(args.output).resolve(), payload)
    elif args.command == "aggregate":
        payload = aggregate_aa_bundle(
            artifacts_root=Path(args.artifacts_root),
            run_map_path=Path(args.run_map),
            output_root=Path(args.output_root),
        )
    else:
        payload = import_aa_bundle(
            bundle_root=Path(args.bundle_root),
            output=Path(args.output),
        )
    _write_stdout(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
