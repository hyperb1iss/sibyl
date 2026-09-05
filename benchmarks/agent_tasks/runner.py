"""Run one trusted development task and independently check its final snapshot.

Separate same-UID processes prevent accidental workspace mixing, not adversarial
access. Receipts explicitly make no sealed-isolation or learning-benefit claim.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchmarks.agent_tasks.manifest import (
    FIXED_ENVIRONMENT,
    Arm,
    ControllerBudget,
    FrozenModel,
    Manifest,
    ManifestError,
    Task,
    canonical_bytes,
    digest,
    identity,
    load_manifest,
    runtime_identity,
    strict_json,
)
from pydantic import Field

PROCESS_TABLE_COLUMNS = 2


class UnsafeSnapshotError(ValueError):
    """A workspace cannot be copied completely and faithfully."""


class ProtocolError(ValueError):
    """A controller or checker returned an invalid protocol object."""

    def __init__(self, role: str, message: str) -> None:
        super().__init__(message)
        self.status = f"{role}_protocol_invalid"


class ControllerResult(FrozenModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tool_calls: int | None = Field(default=None, ge=0)
    model: str | None = None
    synthetic: bool


class CheckerResult(FrozenModel):
    passed: bool
    detail: str


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        handle.write(canonical_bytes(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _put(path: Path, content: bytes, mode: int = 420) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
    path.chmod(mode)


def _snapshot_entries(directory: Path) -> Iterator[Path]:
    # glob/rglob suppress subtree enumeration errors. A hidden unreadable file
    # must not disappear symmetrically from both sides of a parity comparison.
    with os.scandir(directory) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    for entry in entries:
        yield Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            yield from _snapshot_entries(Path(entry.path))


def snapshot(root: Path) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Read every regular file and directory, failing on unreadable subtrees."""
    try:
        return _read_snapshot(root)
    except OSError as exc:
        raise UnsafeSnapshotError(f"cannot read complete task snapshot: {exc}") from exc


def _read_snapshot(root: Path) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    if root.is_symlink() or not root.is_dir():
        raise UnsafeSnapshotError("snapshot root is not a real directory")
    entries: list[dict[str, Any]] = [
        {"path": ".", "kind": "directory", "mode": stat.S_IMODE(root.stat().st_mode)}
    ]
    contents: dict[str, bytes] = {}
    for path in _snapshot_entries(root):
        mode = path.lstat().st_mode
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(mode):
            entries.append({"path": relative, "kind": "directory", "mode": stat.S_IMODE(mode)})
            continue
        if not stat.S_ISREG(mode) or path.is_symlink():
            raise UnsafeSnapshotError("task snapshot contains a symlink or special file")
        content = path.read_bytes()
        permissions = stat.S_IMODE(mode)
        if permissions & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise UnsafeSnapshotError("task snapshot contains special permissions")
        entries.append(
            {"path": relative, "kind": "file", "sha256": digest(content), "mode": permissions}
        )
        contents[relative] = content
    return entries, contents


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _group_quiescent(group_id: int, timeout: float = 5.0) -> bool:
    """Zombies cannot mutate files; any other owned-group member blocks snapshots."""
    deadline = time.monotonic() + timeout
    while True:
        processes = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,stat="],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            env=FIXED_ENVIRONMENT,
        )
        live = any(
            fields[0] == str(group_id) and not fields[1].startswith("Z")
            for line in processes.stdout.splitlines()
            if len(fields := line.split()) == PROCESS_TABLE_COLUMNS
        )
        if not live:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _execute(
    *,
    program: Path,
    program_sha256: str,
    args: list[str],
    request: dict[str, Any],
    workspace: Path,
    output: Path,
    role: str,
    timeout: float,
) -> dict[str, Any]:
    if digest(program.read_bytes()) != program_sha256:
        raise ManifestError("staged program differs from the frozen manifest")
    started = time.monotonic()
    environment = {
        **FIXED_ENVIRONMENT,
        "PATH": str(Path(sys.executable).resolve().parent),
        "HOME": str(output / f"{role}-home"),
        "TMPDIR": str(output / f"{role}-tmp"),
    }
    Path(environment["HOME"]).mkdir()
    Path(environment["TMPDIR"]).mkdir()
    request_path = output / f"{role}-request.json"
    _write_json(request_path, request)
    timed_out = False
    with (
        request_path.open("rb") as stdin,
        (output / f"{role}-stdout.txt").open("xb") as stdout,
        (output / f"{role}-stderr.txt").open("xb") as stderr,
    ):
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-I", str(program), *args],
            cwd=workspace,
            env=environment,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # The parent may exit before its children. Stop the owned group
            # before taking the artifact snapshot on either success or failure.
            _kill_group(process.pid)
            process.wait()
    return {
        "process_group_quiescent": _group_quiescent(process.pid),
        "returncode": process.returncode,
        "timed_out": timed_out,
        "elapsed_seconds": time.monotonic() - started,
        "stdout_sha256": digest((output / f"{role}-stdout.txt").read_bytes()),
        "stderr_sha256": digest((output / f"{role}-stderr.txt").read_bytes()),
        "request_sha256": digest(request_path.read_bytes()),
    }


def _read_protocol[Model: FrozenModel](output: Path, role: str, schema: type[Model]) -> Model:
    try:
        return schema.model_validate(strict_json((output / f"{role}-stdout.txt").read_bytes()))
    except (OSError, ValueError) as exc:
        raise ProtocolError(role, str(exc)) from exc


def _controller_usage(output: Path) -> dict[str, Any]:
    result = _read_protocol(output, "controller", ControllerResult)
    complete = all(
        value is not None
        for value in (result.input_tokens, result.output_tokens, result.tool_calls, result.cost_usd)
    )
    return {**result.model_dump(), "complete": complete, "provenance": "controller_reported"}


def _checked_snapshot(output: Path, workspace: Path) -> str:
    inventory, contents = snapshot(workspace)
    _write_json(output / "final-snapshot.json", inventory)
    destination = output / "checker-workspace"
    destination.mkdir()
    for item in inventory:
        path = destination / item["path"]
        if item["kind"] == "directory":
            path.mkdir(parents=True, exist_ok=True)
        else:
            _put(path, contents[item["path"]], item["mode"])
    # Set directory modes after writes so read-only directories remain copyable.
    for item in reversed(inventory):
        if item["kind"] == "directory":
            (destination / item["path"]).chmod(item["mode"])
    copied, _ = snapshot(destination)
    if copied != inventory:
        raise UnsafeSnapshotError("checker snapshot differs from controller final state")
    return identity(inventory)


def _receipt(manifest: Manifest, task: Task, arm: Arm) -> dict[str, Any]:
    native_payload = arm.native_render_payload
    provenance_status = "none" if arm.memory_pack.sha256 == digest(b"") else "unattributed"
    if native_payload is not None:
        provenance_status = "native_render_v1"
    return {
        "schema_version": "sibyl-agent-task-receipt-v1",
        "purpose": "trusted_development",
        "experiment_id": manifest.experiment_id,
        "comparison_group": "manifest_sha256 and task_sha256",
        "sealed_isolation": False,
        "learning_benefit_established": False,
        "process_isolation": "owned_process_group_only_no_detached_process_guarantee",
        "run_id": uuid4().hex,
        "attempt_id": uuid4().hex,
        "request_id": uuid4().hex,
        "manifest_sha256": identity(manifest.model_dump(mode="json")),
        "task_id": task.id,
        "task_sha256": identity(task.model_dump(mode="json")),
        "arm_id": arm.id,
        "arm_sha256": identity(arm.model_dump(mode="json")),
        "pack_id": arm.memory_pack.sha256,
        "memory_pack_sha256": arm.memory_pack.sha256,
        "memory_provenance": {
            "status": provenance_status,
            "native_payload_sha256": native_payload.sha256 if native_payload else None,
            "render_schema_version": "sibyl-context-render-v1" if native_payload else None,
        },
        "runtime": runtime_identity(),
        "runner_source_sha256": {
            name: digest(Path(__file__).with_name(name).read_bytes())
            for name in ("manifest.py", "runner.py", "__main__.py", "__init__.py")
        },
        "seed": manifest.seed,
        "controller_identity_provenance": "hash_bound_script_declared_model_tools",
        "seed_enforcement": "controller_request_only",
        "controller_model": manifest.controller_model,
        "controller_tools": manifest.controller_tools,
        "controller_budget": manifest.controller_budget.model_dump(),
        "budget_enforcement": "wall_time_enforced_other_budgets_checked_from_controller_report",
        "budget_status": "unknown",
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "success": False,
        "usage": {
            "input_tokens": None,
            "output_tokens": None,
            "tool_calls": None,
            "cost_usd": None,
            "complete": False,
            "provenance": "unavailable",
        },
    }


def _budget_status(usage: dict[str, Any], budget: ControllerBudget) -> str:
    limits = budget.model_dump()
    if any(usage[key] is not None and usage[key] > limit for key, limit in limits.items()):
        return "exceeded"
    return "within_reported_budget" if usage["complete"] else "unknown"


def _process_failure(process: dict[str, Any], role: str) -> str | None:
    if not process["process_group_quiescent"]:
        return f"{role}_cleanup_failed"
    if process["timed_out"]:
        return f"{role}_timeout"
    if process["returncode"] != 0:
        return f"{role}_failed"
    return None


def _check_task(manifest: Manifest, task: Task, output: Path, receipt: dict[str, Any]) -> None:
    receipt["usage"] = _controller_usage(output)
    if receipt["usage"]["model"] not in (None, manifest.controller_model):
        raise ProtocolError("controller", "controller reported a different model than the manifest")
    receipt["budget_status"] = _budget_status(receipt["usage"], manifest.controller_budget)
    checker = _execute(
        program=output / "inputs" / task.checker.script.path,
        program_sha256=task.checker.script.sha256,
        args=task.checker.args,
        request={
            "attempt_id": receipt["attempt_id"],
            "snapshot_sha256": receipt["checker_input_snapshot_sha256"],
        },
        workspace=output / "checker-workspace",
        output=output,
        role="checker",
        timeout=manifest.checker_timeout_seconds,
    )
    receipt["checker"] = checker
    if failure := _process_failure(checker, "checker"):
        receipt["status"] = failure
        return
    result = _read_protocol(output, "checker", CheckerResult)
    receipt["outcome"] = result.model_dump()
    receipt["success"] = result.passed and receipt["budget_status"] != "exceeded"
    receipt["status"] = "passed" if result.passed else "task_failed"
    if receipt["budget_status"] == "exceeded":
        receipt["status"] = "controller_budget_exceeded"


def _perform_attempt(
    manifest: Manifest,
    task: Task,
    arm: Arm,
    inputs: dict[str, bytes],
    output: Path,
    receipt: dict[str, Any],
) -> None:
    _write_json(output / "manifest.json", manifest.model_dump(mode="json"))
    for name, content in inputs.items():
        _put(output / "inputs" / name, content, 292)
    workspace = output / "controller-workspace"
    workspace.mkdir()
    for item in task.workspace:
        _put(workspace / item.destination, inputs[item.artifact.path], item.mode)
    initial, _ = snapshot(workspace)
    _write_json(output / "initial-snapshot.json", initial)
    request = {
        **{
            key: receipt[key]
            for key in (
                "run_id",
                "attempt_id",
                "request_id",
                "task_id",
                "task_sha256",
                "pack_id",
                "seed",
                "controller_model",
                "controller_tools",
                "controller_budget",
            )
        },
        "prompt": inputs[task.prompt.path].decode(),
        "memory_pack": inputs[arm.memory_pack.path].decode(),
        "memory_pack_sha256": arm.memory_pack.sha256,
    }
    controller = _execute(
        program=output / "inputs" / manifest.controller.script.path,
        program_sha256=manifest.controller.script.sha256,
        args=manifest.controller.args,
        request=request,
        workspace=workspace,
        output=output,
        role="controller",
        timeout=manifest.controller_timeout_seconds,
    )
    receipt["controller"] = controller
    _write_json(output / "receipt.json", receipt)
    if not controller["process_group_quiescent"]:
        receipt["status"] = "controller_cleanup_failed"
        return
    final_hash = _checked_snapshot(output, workspace)
    receipt["controller_final_snapshot_sha256"] = final_hash
    receipt["checker_input_snapshot_sha256"] = final_hash
    if failure := _process_failure(controller, "controller"):
        receipt["status"] = failure
        return
    _check_task(manifest, task, output, receipt)


def run_task(manifest_path: Path, *, task_id: str, arm_id: str, output: Path) -> dict[str, Any]:
    """Validate first; create one exclusive attempt with durable partial receipts."""
    if os.name != "posix":
        raise ManifestError("the trusted development adapter requires POSIX process groups")
    manifest, inputs = load_manifest(manifest_path)
    task = next((task for task in manifest.tasks if task.id == task_id), None)
    arm = next((arm for arm in manifest.arms if arm.id == arm_id), None)
    if task is None or arm is None:
        raise ManifestError("unknown task or arm")
    if task.split != "development":
        raise ManifestError(
            "sealed execution requires an isolated agent runtime; this adapter is development-only"
        )
    output = output.absolute()
    if output.is_symlink():
        raise ManifestError("attempt output must not be a symlink")
    output = output.parent.resolve() / output.name
    if output.is_relative_to(manifest_path.parent.resolve()):
        raise ManifestError("attempt output must be outside the frozen input directory")
    output.mkdir(parents=False, exist_ok=False)
    receipt = _receipt(manifest, task, arm)
    _write_json(output / "receipt.json", receipt)
    try:
        _perform_attempt(manifest, task, arm, inputs, output, receipt)
    except (ProtocolError, UnsafeSnapshotError) as exc:
        receipt["status"] = exc.status if isinstance(exc, ProtocolError) else "unsafe_snapshot"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        receipt["status"] = "runner_error"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        receipt["completed_at"] = datetime.now(UTC).isoformat()
        receipt["receipt_sha256"] = identity(receipt)
        _write_json(output / "receipt.json", receipt)
    return receipt
