"""Redacted append-only subprocess logging for local release work."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_state as state
from tools.bench import longmemeval_v2_rig as rig

ROOT = Path(__file__).resolve().parents[1]
FD_EXEC = ROOT / "benchmarks/longmemeval_v2_release_fd_exec.py"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


def _owned_duplicate(descriptor: int, identity: tuple[int, int], *, name: str) -> int:
    try:
        duplicate = os.dup(descriptor)
    except OSError as exc:
        raise OSError(f"{name} descriptor is closed") from exc
    try:
        metadata = os.fstat(duplicate)
        valid = (metadata.st_dev, metadata.st_ino) == identity and stat.S_ISDIR(metadata.st_mode)
    except BaseException:
        os.close(duplicate)
        raise
    if not valid:
        os.close(duplicate)
        raise OSError(f"{name} descriptor identity changed")
    return duplicate


def _sandbox_profile(root: Path) -> str:
    if not root.is_absolute() or '"' in str(root) or "\n" in str(root):
        raise OSError("package sandbox root is not canonical")
    return f'(version 1) (allow default) (deny file-write*) (allow file-write* (subpath "{root}"))'


def _append_log(
    path: Path,
    payload: dict[str, Any],
    *,
    directory_fd: int | None,
    name: str | None,
) -> None:
    if directory_fd is None:
        state.append_log(path, payload)
        return
    if name is None or Path(name).name != name:
        raise ValueError("fd-owned command log requires one safe relative name")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("fd-owned command log is not a regular file")
        content = (json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n").encode()
        while content:
            written = os.write(descriptor, content)
            content = content[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _run_process(
    command: list[str],
    *,
    log_path: Path,
    secrets: tuple[str, ...],
    working_directory_path: Path | None = None,
    owned_working_fd: int | None = None,
    owned_log_fd: int | None = None,
    log_name: str | None = None,
) -> int:
    _append_log(
        log_path,
        {
            "event": "start",
            "recorded_at": state.now(),
            "command_sha256": rig.canonical_sha256(command),
        },
        directory_fd=owned_log_fd,
        name=log_name,
    )
    actual_command = command
    pass_fds: tuple[int, ...] = ()
    if owned_working_fd is not None and working_directory_path is not None:
        actual_command = [
            str(SANDBOX_EXEC),
            "-p",
            _sandbox_profile(working_directory_path),
            sys.executable,
            str(FD_EXEC),
            str(owned_working_fd),
            "--",
            *command,
        ]
        pass_fds = (owned_working_fd,)
    try:
        process = subprocess.Popen(  # noqa: S603
            actual_command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            pass_fds=pass_fds,
        )
    except OSError as exc:
        _append_log(
            log_path,
            {"event": "output", "text": state.redact(exc, secrets=secrets)},
            directory_fd=owned_log_fd,
            name=log_name,
        )
        _append_log(
            log_path,
            {"event": "exit", "recorded_at": state.now(), "returncode": -1},
            directory_fd=owned_log_fd,
            name=log_name,
        )
        return -1
    assert process.stdout is not None
    for line in process.stdout:
        _append_log(
            log_path,
            {"event": "output", "text": state.redact(line.rstrip(), secrets=secrets)},
            directory_fd=owned_log_fd,
            name=log_name,
        )
    returncode = process.wait()
    _append_log(
        log_path,
        {"event": "exit", "recorded_at": state.now(), "returncode": returncode},
        directory_fd=owned_log_fd,
        name=log_name,
    )
    return returncode


def invoke_command(
    command: list[str],
    *,
    log_path: Path,
    secrets: tuple[str, ...],
    working_directory_fd: int | None = None,
    working_directory_identity: tuple[int, int] | None = None,
    working_directory_path: Path | None = None,
    log_directory_fd: int | None = None,
    log_directory_identity: tuple[int, int] | None = None,
    log_name: str | None = None,
) -> int:
    """Run one command while recording only its digest and redacted output."""

    owned_working_fd: int | None = None
    owned_log_fd: int | None = None
    if working_directory_fd is not None:
        if (
            working_directory_identity is None
            or working_directory_path is None
            or log_directory_fd is None
            or log_directory_identity is None
        ):
            raise OSError("fd-owned command invocation is incomplete")
        if sys.platform != "darwin" or not SANDBOX_EXEC.is_file():
            raise OSError("fd-owned package commands require the macOS sandbox")
        owned_working_fd = _owned_duplicate(
            working_directory_fd,
            working_directory_identity,
            name="package working directory",
        )
        try:
            owned_log_fd = _owned_duplicate(
                log_directory_fd,
                log_directory_identity,
                name="package log directory",
            )
        except BaseException:
            os.close(owned_working_fd)
            raise
    try:
        return _run_process(
            command,
            log_path=log_path,
            secrets=secrets,
            working_directory_path=working_directory_path,
            owned_working_fd=owned_working_fd,
            owned_log_fd=owned_log_fd if owned_log_fd is not None else log_directory_fd,
            log_name=log_name,
        )
    finally:
        if owned_working_fd is not None:
            os.close(owned_working_fd)
        if owned_log_fd is not None:
            os.close(owned_log_fd)
