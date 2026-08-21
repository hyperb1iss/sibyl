"""Validate the single published authority for one official release arm."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_official_package as package
from benchmarks import longmemeval_v2_release_package_archive as package_archive
from benchmarks import longmemeval_v2_release_package_inventory as package_inventory
from benchmarks import longmemeval_v2_release_package_object as package_object
from benchmarks import longmemeval_v2_release_package_process as process
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks.longmemeval_v2_release_handoff import ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    require_exact_keys,
)
from tools.bench import longmemeval_v2_rig as rig


def package_official_arm(
    executed: ExecutedStage,
    *,
    arm_id: str,
    packages_root: Path,
) -> dict[str, Any]:
    """Publish one arm and return only its canonical validated authority."""

    package.build_official_arm_publication(
        executed,
        arm_id=arm_id,
        packages_root=packages_root,
    )
    return require_official_arm_package(
        executed,
        arm_id=arm_id,
        packages_root=packages_root,
    )


def require_official_arm_package(
    executed: ExecutedStage,
    *,
    arm_id: str,
    packages_root: Path,
) -> dict[str, Any]:
    """Validate the single publication authority for one packaged arm."""

    run = package._find_run(executed, arm_id)
    parent_owner = package_root.bind_packages_root(
        executed.plan,
        executed.runs,
        packages_root,
    )
    with (
        package_root.publication_lock(parent_owner),
        _open_locked_publication(
            executed,
            run,
            arm_id,
            parent_owner,
            packaging_status=None,
            expected=None,
        ) as result,
    ):
        return result


def require_claimed_official_arm_package(
    executed: ExecutedStage,
    *,
    arm_id: str,
    packages_root: Path,
    expected: dict[str, Any],
    packaging_status: dict[str, Any],
) -> dict[str, Any]:
    """Validate a claimed immutable arm during package-lifecycle resume."""

    with open_claimed_official_arm_package(
        executed,
        arm_id=arm_id,
        packages_root=packages_root,
        expected=expected,
        packaging_status=packaging_status,
    ) as result:
        return result


@contextmanager
def open_claimed_official_arm_package(
    executed: ExecutedStage,
    *,
    arm_id: str,
    packages_root: Path,
    expected: dict[str, Any],
    packaging_status: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Retain canonical arm authority handles through score-aware semantics."""

    package.require_claimed_executed_snapshot(executed, packaging_status)
    run = package._find_run(executed, arm_id)
    parent_owner = package_root.bind_packages_root(
        executed.plan,
        executed.runs,
        packages_root,
    )
    with (
        package_root.publication_lock(parent_owner),
        _open_locked_publication(
            executed,
            run,
            arm_id,
            parent_owner,
            packaging_status=packaging_status,
            expected=expected,
        ) as result,
    ):
        yield result


def _require_external_snapshot(
    executed: ExecutedStage,
    packaging_status: dict[str, Any] | None,
) -> None:
    if packaging_status is None:
        package._require_executed_snapshot(executed)
    else:
        package.require_claimed_executed_snapshot(executed, packaging_status)


@contextmanager
def _open_locked_publication(
    executed: ExecutedStage,
    run: dict[str, Any],
    arm_id: str,
    parent_owner: package_root.OwnedDirectory,
    *,
    packaging_status: dict[str, Any] | None,
    expected: dict[str, Any] | None,
) -> Iterator[dict[str, Any]]:
    """Consume one authority while sibling metadata cannot be thawed."""

    parent_fd = package_root.open_owned_directory(
        parent_owner,
        name="official packages root",
    )
    try:
        publications = package_inventory.require_publication_inventory(
            parent_owner,
            parent_fd,
            arm_ids={candidate["arm_id"] for candidate in executed.runs},
        )
        raw = publications.get(arm_id)
        if raw is None:
            raise StagePlanError("official arm publication is missing")
        arm_owner, arm_fd = package_object.open_arm_authority(
            parent_owner,
            parent_fd,
            arm_id,
        )
        handle: package_object.AuthorityHandle | None = None
        try:
            live_raw, content, handle = package_object.read_arm_authority(
                arm_owner,
                arm_fd,
                packages_root=parent_owner.path,
                arm_id=arm_id,
            )
            if live_raw != raw:
                raise StagePlanError("official arm publication changed during validation")
            status_binding = dict(executed.control_artifacts).get("runner_status")
            if not isinstance(status_binding, dict):
                raise StagePlanError("published executed status binding is missing")
            result = _require_publication_authority(
                executed,
                run,
                raw,
                content,
                executed_status=status_binding,
            )
            if expected is not None and result != expected:
                raise StagePlanError("official arm publication differs from its package claim")
            package_object.require_arm_authority_unchanged(arm_fd, handle)
            _require_external_snapshot(executed, packaging_status)
            yield result
            package_object.require_arm_authority_unchanged(arm_fd, handle)
            _require_external_snapshot(executed, packaging_status)
        finally:
            if handle is not None:
                handle.close()
            error = package_root.close_owned_directory(
                arm_fd,
                arm_owner,
                name="official arm authority",
            )
            if error is not None:
                raise StagePlanError("official arm descriptor ownership changed") from error
    finally:
        error = package_root.close_owned_directory(
            parent_fd,
            parent_owner,
            name="official packages root",
        )
        if error is not None:
            raise StagePlanError("official packages descriptor ownership changed") from error


def _member_json(members: dict[str, bytes], name: str) -> dict[str, Any]:
    content = members.get(name)
    if content is None:
        raise StagePlanError(f"official package member {name!r} is missing")
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagePlanError(f"official package member {name!r} is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise StagePlanError(f"official package member {name!r} is not an object")
    return raw


def _require_member_binding(
    binding: object,
    *,
    name: str,
    members: dict[str, bytes],
) -> None:
    if not isinstance(binding, dict) or name not in members:
        raise StagePlanError("official package member binding is invalid")
    path = binding.get("path")
    if not isinstance(path, str) or not (path == name or path.endswith(f"/{name}")):
        raise StagePlanError("official package member binding path changed")
    expected = package_archive.member_binding(path, members[name])
    if binding != expected:
        raise StagePlanError("official package member binding changed")


def _require_command_members(
    members: dict[str, bytes],
    commands: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    for step in package.PACKAGE_STEPS:
        name = f"command_receipts/{step}.json"
        raw = _member_json(members, name)
        require_exact_keys(raw, process.COMMAND_RECEIPT_KEYS, name="package command receipt")
        unsigned = {key: value for key, value in raw.items() if key != "command_receipt_sha256"}
        if (
            raw.get("schema_version") != process.COMMAND_RECEIPT_SCHEMA_VERSION
            or raw.get("step") != step
            or raw.get("command_sha256") != rig.canonical_sha256(commands[step])
            or raw.get("returncode") != 0
            or raw.get("status") != "COMPLETE"
            or raw.get("error") is not None
            or raw.get("command_receipt_sha256") != rig.canonical_sha256(unsigned)
        ):
            raise StagePlanError("published package command receipt is invalid")
        _require_member_binding(
            raw.get("log"),
            name=f"logs/{step}.jsonl",
            members=members,
        )
        outputs = raw.get("outputs")
        if not isinstance(outputs, dict):
            raise StagePlanError("published package command outputs are invalid")
        for binding in outputs.values():
            if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
                raise StagePlanError("published package command output is invalid")
            matches = [
                member
                for member in members
                if binding["path"] == member or binding["path"].endswith(f"/{member}")
            ]
            if len(matches) != 1:
                raise StagePlanError("published package command output path changed")
            _require_member_binding(binding, name=matches[0], members=members)
        receipts[step] = raw
    return receipts


def _require_arm_package(
    executed: ExecutedStage,
    run: dict[str, Any],
    members: dict[str, bytes],
    command_receipts: dict[str, dict[str, Any]],
    *,
    actual_cost: float,
) -> None:
    arm_package = _member_json(members, "arm_package.json")
    require_exact_keys(arm_package, package.ARM_PACKAGE_KEYS, name="official arm package")
    unsigned = {key: value for key, value in arm_package.items() if key != "arm_package_sha256"}
    if (
        arm_package.get("schema_version") != package.ARM_PACKAGE_SCHEMA_VERSION
        or arm_package.get("stage_plan_sha256") != executed.plan["stage_plan_sha256"]
        or arm_package.get("arm_id") != run["arm_id"]
        or arm_package.get("status") != "PASS"
        or arm_package.get("actual_cost_usd") != actual_cost
        or arm_package.get("arm_package_sha256") != rig.canonical_sha256(unsigned)
    ):
        raise StagePlanError("published official arm package is invalid")
    receipt_bindings = arm_package.get("command_receipts")
    if not isinstance(receipt_bindings, dict) or set(receipt_bindings) != set(command_receipts):
        raise StagePlanError("published official arm command receipt set changed")
    for step, binding in receipt_bindings.items():
        _require_member_binding(
            binding,
            name=f"command_receipts/{step}.json",
            members=members,
        )
    _require_member_binding(
        arm_package.get("combined_official_receipt"),
        name="combined/longmemeval_v2_official_receipt.json",
        members=members,
    )
    _require_member_binding(arm_package.get("arm_run"), name="arm_run.json", members=members)


def _require_publication_authority(
    executed: ExecutedStage,
    run: dict[str, Any],
    raw: dict[str, Any],
    content: bytes,
    *,
    executed_status: dict[str, Any],
) -> dict[str, Any]:
    package_object.require_publication_receipt(raw)
    actual_cost = package._arm_cost(executed, run)
    if (
        raw.get("stage_plan_sha256") != executed.plan["stage_plan_sha256"]
        or raw.get("arm_id") != run["arm_id"]
        or raw.get("actual_cost_usd") != actual_cost
        or raw.get("executed_status") != executed_status
    ):
        raise StagePlanError("official arm publication lineage is invalid")
    object_binding = raw.get("package_object")
    if not isinstance(object_binding, dict) or not isinstance(object_binding.get("sha256"), str):
        raise StagePlanError("official arm publication object binding is invalid")
    binding_path = object_binding.get("path")
    if (
        not isinstance(binding_path, str)
        or package_tree.bind_owned_content(content, path=Path(binding_path)) != object_binding
    ):
        raise StagePlanError("official arm publication object changed")
    members, manifest = package_archive.require_package_object(content)
    if raw.get("package_manifest_sha256") != manifest["package_manifest_sha256"]:
        raise StagePlanError("official arm publication manifest changed")
    adapter_name = Path(executed.plan["package_inputs"]["adapter"]["path"]).name
    staging_root = Path("/staging")
    commands, paths = package._step_commands(executed, run, staging_root)
    expected_members = {
        path.relative_to(staging_root).as_posix()
        for path in package._final_package_files(
            staging_root,
            paths,
            adapter_name=adapter_name,
        )
    }
    if set(members) != expected_members:
        raise StagePlanError("official arm publication inventory is not exact")
    command_receipts = _require_command_members(members, commands)
    _require_arm_package(
        executed,
        run,
        members,
        command_receipts,
        actual_cost=actual_cost,
    )
    _require_member_binding(raw.get("arm_run"), name="arm_run.json", members=members)
    _require_member_binding(raw.get("arm_package"), name="arm_package.json", members=members)
    return raw
