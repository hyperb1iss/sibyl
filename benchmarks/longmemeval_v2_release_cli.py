"""Operator CLI for one sealed LongMemEval-V2 release stage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_official_publication as official_publication
from benchmarks import longmemeval_v2_release_package as release_package
from benchmarks import longmemeval_v2_release_package_object as package_object
from benchmarks import longmemeval_v2_release_package_policy as package_policy
from benchmarks import longmemeval_v2_release_plan as release_plan
from benchmarks import longmemeval_v2_release_runner as release_runner
from benchmarks.longmemeval_v2_release_contract import MAX_WORKERS_CAP
from benchmarks.longmemeval_v2_release_handoff import require_executed_stage
from benchmarks.longmemeval_v2_release_inputs import StagePlanError, load_json

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_DESCRIPTION = ROOT / "benchmarks" / "longmemeval_v2_release_assets" / "SYSTEM_DESCRIPTION.md"
ADAPTER = ROOT / "benchmarks" / "longmemeval_v2_memory" / "sibyl_memory.py"


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _canonical_destination(value: str, *, name: str) -> Path:
    return package_policy.canonical_path(Path(value), name=name)


def _require_plan_destinations(
    *,
    output_root: Path,
    plan_output: Path,
    spec_path: Path,
    official_repo: Path,
    data_root: Path,
) -> None:
    sealed_inputs = {
        ROOT.resolve(),
        official_repo,
        data_root,
        spec_path,
        SYSTEM_DESCRIPTION.resolve(),
        ADAPTER.resolve(),
    }
    for destination_name, destination in (
        ("stage output root", output_root),
        ("stage plan path", plan_output),
    ):
        if any(package_policy.overlaps(destination, sealed) for sealed in sealed_inputs):
            raise StagePlanError(f"{destination_name} overlaps a sealed release input")
    if package_policy.overlaps(output_root, plan_output):
        raise StagePlanError("stage plan path overlaps its fresh output root")


def _object(path: Path, *, name: str) -> dict[str, Any]:
    raw = load_json(path)
    if not isinstance(raw, dict):
        raise StagePlanError(f"{name} must be a JSON object")
    return raw


def _supported_plan(path: Path) -> dict[str, Any]:
    plan = _object(path, name="release stage plan")
    release_plan.require_current_release_host(plan)
    return plan


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--spec", required=True)
    parser.add_argument("--official-repo", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--output", required=True)


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", required=True)
    parser.add_argument(
        "--max-workers",
        type=int,
        choices=range(1, MAX_WORKERS_CAP + 1),
        default=MAX_WORKERS_CAP,
        help="temporary preregistered local-machine worker bound",
    )


def _add_arm_package_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", required=True)
    parser.add_argument("--arm-id", required=True)
    parser.add_argument("--packages-root", required=True)


def _add_package_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", required=True)
    parser.add_argument("--packages-root", required=True)
    parser.add_argument("--preregistration-template")


def add_release_arguments(subparsers: Any) -> None:
    """Register the isolated v1.3 release commands on the benchmark CLI."""

    _add_plan_arguments(subparsers.add_parser("release-plan"))
    _add_run_arguments(subparsers.add_parser("release-run"))
    _add_arm_package_arguments(subparsers.add_parser("release-arm-package"))
    _add_package_arguments(subparsers.add_parser("release-package"))
    verify = subparsers.add_parser("release-verify")
    verify.add_argument("--plan", required=True)


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    spec_path = _path(args.spec)
    official_repo = _path(args.official_repo)
    data_root = _path(args.data_root)
    output_root = _canonical_destination(args.output_root, name="stage output root")
    plan_output = _canonical_destination(args.output, name="stage plan path")
    _require_plan_destinations(
        output_root=output_root,
        plan_output=plan_output,
        spec_path=spec_path,
        official_repo=official_repo,
        data_root=data_root,
    )
    payload = release_plan.build_stage_plan(
        spec=_object(spec_path, name="release stage spec"),
        spec_path=spec_path,
        official_repo=official_repo,
        data_root=data_root,
        output_root=output_root,
        system_description_path=SYSTEM_DESCRIPTION,
        adapter_path=ADAPTER,
        release_host=package_policy.probe_release_host(output_root),
    )
    release_plan.write_stage_plan(plan_output, payload)
    return payload


def _run(args: argparse.Namespace) -> dict[str, Any]:
    payload = release_runner.run_stage_plan(
        _supported_plan(_path(args.plan)),
        max_workers=args.max_workers,
    )
    if payload.get("status") != "EXECUTED":
        raise StagePlanError("release runner did not produce an EXECUTED status")
    return payload


def _package_arm(args: argparse.Namespace) -> dict[str, Any]:
    executed = require_executed_stage(_supported_plan(_path(args.plan)))
    if args.arm_id not in {run["arm_id"] for run in executed.runs}:
        raise StagePlanError("official arm ID is not present in the sealed stage")
    packages_root = _path(args.packages_root)
    authority_path = package_object.publication_path(packages_root, args.arm_id)
    package_arm = (
        official_publication.require_official_arm_package
        if authority_path.exists() or authority_path.is_symlink()
        else official_publication.package_official_arm
    )
    payload = package_arm(executed, arm_id=args.arm_id, packages_root=packages_root)
    if payload.get("status") != "PASS":
        raise StagePlanError("official arm packaging did not produce a PASS authority")
    return payload


def _package(args: argparse.Namespace) -> dict[str, Any]:
    template = (
        _path(args.preregistration_template) if args.preregistration_template is not None else None
    )
    return release_package.package_stage(
        _supported_plan(_path(args.plan)),
        official_packages_root=_path(args.packages_root),
        preregistration_template=template,
    )


def _verify(args: argparse.Namespace) -> dict[str, Any]:
    return release_package.require_packaged_stage(_supported_plan(_path(args.plan)))


def run_release_cli_command(args: argparse.Namespace) -> int:
    """Run exactly the named release phase and print its canonical receipt."""

    handlers = {
        "release-plan": _plan,
        "release-run": _run,
        "release-arm-package": _package_arm,
        "release-package": _package,
        "release-verify": _verify,
    }
    try:
        handler = handlers[args.command]
    except KeyError as exc:
        raise RuntimeError(f"Unknown release command: {args.command}") from exc
    _emit(handler(args))
    return 0
