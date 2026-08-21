"""Audited subprocess transactions for LongMemEval-V2 release packaging."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_command import invoke_command as _invoke_command
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    bind_artifact,
    load_json,
    require_artifact,
    require_exact_keys,
)
from tools.bench import longmemeval_v2_rig as rig

COMMAND_RECEIPT_SCHEMA_VERSION = "sibyl-longmemeval-v2-package-command-v1"
COMMAND_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "step",
        "command_sha256",
        "returncode",
        "status",
        "log",
        "outputs",
        "error",
        "started_at",
        "completed_at",
        "command_receipt_sha256",
    }
)


@dataclass(frozen=True)
class CompletedCommand:
    """One immutable package command plus its validated receipt."""

    command: tuple[str, ...]
    receipt: dict[str, Any]


def owned_binding(path: Path, *, root: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or path.resolve() != path or not path.is_relative_to(root):
        raise StagePlanError(f"{name} is not one canonical package-owned file")
    return bind_artifact(path, name=name)


def exact_tree_bindings(
    root: Path,
    *,
    package_root: Path,
    prefix: str,
    relative_files: frozenset[str],
) -> dict[str, Any]:
    """Bind an exact, declared output tree without blessing foreign files."""

    expected = {root / relative for relative in relative_files}
    require_inventory(root, expected)
    return {
        f"{prefix}:{relative}": owned_binding(
            root / relative,
            root=package_root,
            name=f"{prefix} output {relative}",
        )
        for relative in sorted(relative_files)
    }


def log_path(root: Path, step: str) -> Path:
    return root / "logs" / f"{step}.jsonl"


def command_receipt_path(root: Path, step: str) -> Path:
    return root / "command_receipts" / f"{step}.json"


def _command_receipt(
    *,
    step: str,
    command: list[str],
    returncode: int,
    log: dict[str, Any],
    outputs: dict[str, Any],
    error: str | None,
    started_at: str,
) -> dict[str, Any]:
    return state.sealed(
        {
            "schema_version": COMMAND_RECEIPT_SCHEMA_VERSION,
            "step": step,
            "command_sha256": rig.canonical_sha256(command),
            "returncode": returncode,
            "status": "COMPLETE" if error is None else "FAIL",
            "log": log,
            "outputs": outputs,
            "error": error,
            "started_at": started_at,
            "completed_at": state.now(),
        },
        "command_receipt_sha256",
    )


def _require_timestamp(value: object) -> None:
    try:
        timestamp = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError as exc:
        raise StagePlanError("package command receipt timestamp is invalid") from exc
    if (
        timestamp is None
        or timestamp.tzinfo is None
        or timestamp.utcoffset() != UTC.utcoffset(None)
        or timestamp.isoformat() != value
    ):
        raise StagePlanError("package command receipt timestamp is invalid")


def _require_error(raw: dict[str, Any], *, status: str, secrets: tuple[str, ...]) -> None:
    error = raw.get("error")
    if status == "FAIL" and (
        not isinstance(error, str) or not error or state.redact(error, secrets=secrets) != error
    ):
        raise StagePlanError("failed package command receipt error is invalid")


def _require_outputs(raw: dict[str, Any], *, package_root: Path) -> None:
    outputs = raw.get("outputs")
    if not isinstance(outputs, dict):
        raise StagePlanError("package command outputs are invalid")
    for name, binding in outputs.items():
        if not isinstance(name, str) or not name:
            raise StagePlanError("package command output name is invalid")
        validated = require_artifact(binding, name=f"package command output {name}")
        if validated != binding or not Path(validated["path"]).is_relative_to(package_root):
            raise StagePlanError("package command output escaped or changed")


def require_command_receipt(
    path: Path,
    *,
    step: str,
    command: list[str],
    package_root: Path,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    if path.is_symlink() or path.resolve() != path or not path.is_relative_to(package_root):
        raise StagePlanError("package command receipt is not one package-owned file")
    raw = load_json(path)
    require_exact_keys(raw, COMMAND_RECEIPT_KEYS, name="package command receipt")
    unsigned = {key: value for key, value in raw.items() if key != "command_receipt_sha256"}
    if (
        raw.get("schema_version") != COMMAND_RECEIPT_SCHEMA_VERSION
        or raw.get("step") != step
        or raw.get("command_sha256") != rig.canonical_sha256(command)
        or raw.get("command_receipt_sha256") != rig.canonical_sha256(unsigned)
    ):
        raise StagePlanError("package command receipt identity is invalid")
    returncode = raw.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise StagePlanError("package command receipt return code is invalid")
    expected_status = "COMPLETE" if returncode == 0 and raw.get("error") is None else "FAIL"
    if raw.get("status") != expected_status:
        raise StagePlanError("package command receipt status is invalid")
    _require_error(raw, status=expected_status, secrets=secrets)
    _require_timestamp(raw.get("started_at"))
    _require_timestamp(raw.get("completed_at"))
    log = state.require_command_log(
        log_path(package_root, step),
        command=command,
        secrets=secrets,
        expected_returncode=returncode,
        expected_invocations=1,
    )
    if raw.get("log") != log:
        raise StagePlanError("package command log changed after completion")
    _require_outputs(raw, package_root=package_root)
    return raw


def receipt_files(root: Path, receipts: dict[str, CompletedCommand]) -> set[Path]:
    files: set[Path] = set()
    for step, completed in receipts.items():
        files.add(log_path(root, step))
        files.add(command_receipt_path(root, step))
        files.update(Path(binding["path"]) for binding in completed.receipt["outputs"].values())
    return files


def require_inventory(root: Path, expected_files: set[Path]) -> None:
    actual_files: set[Path] = set()
    actual_directories: set[Path] = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise StagePlanError("official arm package contains an unsafe path")
        if path.is_file():
            actual_files.add(path)
        elif path.is_dir():
            actual_directories.add(path)
        else:
            raise StagePlanError("official arm package contains a foreign entry")
    expected_directories = {
        parent
        for path in expected_files
        for parent in path.parents
        if parent != root and parent.is_relative_to(root)
    }
    if actual_files != expected_files or actual_directories != expected_directories:
        raise StagePlanError("official arm package inventory is not exact")


def bind_inventory(root: Path, expected_files: set[Path]) -> dict[str, dict[str, Any]]:
    """Validate and bind every file in one exact package inventory."""

    require_inventory(root, expected_files)
    return {
        path.relative_to(root).as_posix(): owned_binding(
            path,
            root=root,
            name=f"official arm package {path.relative_to(root).as_posix()}",
        )
        for path in sorted(expected_files)
    }


def require_receipts(
    root: Path,
    receipts: dict[str, CompletedCommand],
    *,
    secrets: tuple[str, ...],
) -> None:
    for step, completed in receipts.items():
        raw = require_command_receipt(
            command_receipt_path(root, step),
            step=step,
            command=list(completed.command),
            package_root=root,
            secrets=secrets,
        )
        if completed.receipt != raw:
            raise StagePlanError("package command receipt changed during packaging")


def _require_success(returncode: int, *, step: str) -> None:
    if returncode != 0:
        raise StagePlanError(f"official packaging step {step} exited {returncode}")


def execute_step(
    *,
    root: Path,
    step: str,
    command: list[str],
    collect: Callable[[], dict[str, Any]],
    receipts: dict[str, CompletedCommand],
    secrets: tuple[str, ...],
    validate_inputs: Callable[[], None],
    lease: package_root.PackageLease | None = None,
) -> CompletedCommand:
    validate_inputs()
    require_receipts(root, receipts, secrets=secrets)
    started_at = state.now()
    invocation_options: dict[str, Any] = {}
    if lease is not None:
        package_root.require_lease(lease)
        invocation_options = {
            "working_directory_fd": lease.arm_fd,
            "working_directory_identity": (lease.arm.device, lease.arm.inode),
            "working_directory_path": lease.arm.path,
            "log_directory_fd": lease.logs_fd,
            "log_directory_identity": (lease.logs.device, lease.logs.inode),
            "log_name": f"{step}.jsonl",
        }
    returncode = _invoke_command(
        command,
        log_path=log_path(root, step),
        secrets=secrets,
        **invocation_options,
    )
    if lease is not None:
        package_root.require_lease(lease)
    outputs: dict[str, Any] = {}
    error: str | None = None
    try:
        validate_inputs()
        log = state.require_command_log(
            log_path(root, step),
            command=command,
            secrets=secrets,
            expected_returncode=returncode,
            expected_invocations=1,
        )
        _require_success(returncode, step=step)
        outputs = collect()
        require_receipts(root, receipts, secrets=secrets)
        expected = receipt_files(root, receipts) | {
            log_path(root, step),
            *(Path(binding["path"]) for binding in outputs.values()),
        }
        require_inventory(root, expected)
    except Exception as exc:
        error = state.redact(exc, secrets=secrets)
        if lease is None:
            log = state.require_command_log(
                log_path(root, step),
                command=command,
                secrets=secrets,
                expected_returncode=returncode,
                expected_invocations=1,
            )
        else:
            log_content = package_tree.read_owned_file(lease.logs_fd, f"{step}.jsonl")
            try:
                log_text = log_content.decode("utf-8")
            except UnicodeDecodeError as decode_error:
                raise StagePlanError("package command log is not UTF-8") from decode_error
            state.validate_command_log_text(
                log_text,
                command=command,
                secrets=secrets,
                expected_returncode=returncode,
                expected_invocations=1,
            )
            log = package_tree.bind_owned_content(
                log_content,
                path=log_path(root, step),
            )
    payload = _command_receipt(
        step=step,
        command=command,
        returncode=returncode,
        log=log,
        outputs=outputs,
        error=error,
        started_at=started_at,
    )
    if lease is None:
        release_io.write_json_once_atomic(command_receipt_path(root, step), payload)
    else:
        release_io.write_json_once_atomic_at(
            package_root.require_receipts_fd(lease),
            f"{step}.json",
            payload,
        )
    if error is not None:
        raise StagePlanError(error)
    validated = require_command_receipt(
        command_receipt_path(root, step),
        step=step,
        command=command,
        package_root=root,
        secrets=secrets,
    )
    return CompletedCommand(command=tuple(command), receipt=validated)
