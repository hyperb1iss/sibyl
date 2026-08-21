from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_stage_io as stage_io
from benchmarks import longmemeval_v2_release_stage_receipt as stage_receipt
from benchmarks import longmemeval_v2_release_stage_transaction as stage_transaction

OUTPUT_FSTAT_INTERRUPT_CALL = 2


def _assert_closed(descriptor: int) -> None:
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def _reuse_descriptor(descriptor: int, victim_path: Path) -> int:
    os.close(descriptor)
    victim = os.open(victim_path, os.O_RDONLY)
    assert victim == descriptor
    return victim


def test_ensure_directory_interrupt_closes_partially_owned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    transaction = stage_transaction.open_pending_transaction(
        output_root,
        prefix="packages.pending.",
    )
    opened: list[int] = []
    real_open = stage_transaction.stage_io.open_child_directory
    real_stat = stage_transaction.os.stat

    def record_open(parent_fd: int, name: str) -> tuple[int, Any]:
        descriptor, snapshot = real_open(parent_fd, name)
        opened.append(descriptor)
        return descriptor, snapshot

    def interrupt_stat(path: str | Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == "passes" and kwargs.get("dir_fd") == transaction.root_fd:
            raise KeyboardInterrupt
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(stage_transaction.stage_io, "open_child_directory", record_open)
    monkeypatch.setattr(stage_transaction.os, "stat", interrupt_stat)
    try:
        with pytest.raises(KeyboardInterrupt):
            transaction.ensure_directory("passes")
    finally:
        transaction.close()
    assert opened
    _assert_closed(opened[-1])


def test_open_root_interrupt_closes_partially_owned_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages = tmp_path / "packages"
    packages.mkdir()
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    opened: list[int] = []
    real_open = stage_transaction.stage_io.open_child_directory
    real_stat = stage_transaction.os.stat

    def record_open(parent: int, name: str) -> tuple[int, Any]:
        descriptor, snapshot = real_open(parent, name)
        opened.append(descriptor)
        return descriptor, snapshot

    def interrupt_stat(path: str | Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == "packages" and kwargs.get("dir_fd") == parent_fd:
            raise KeyboardInterrupt
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(stage_transaction.stage_io, "open_child_directory", record_open)
    monkeypatch.setattr(stage_transaction.os, "stat", interrupt_stat)
    try:
        with pytest.raises(KeyboardInterrupt):
            stage_transaction._open_root(parent_fd, "packages")
    finally:
        os.close(parent_fd)
    assert opened
    _assert_closed(opened[-1])


def test_receipt_output_fstat_interrupt_closes_owned_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    opened: list[int] = []
    calls: dict[int, int] = {}
    real_open = stage_receipt.os.open
    real_fstat = stage_receipt.os.fstat

    def record_open(path: Any, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if Path(path) == output_root:
            opened.append(descriptor)
        return descriptor

    def interrupt_second(descriptor: int) -> os.stat_result:
        calls[descriptor] = calls.get(descriptor, 0) + 1
        if descriptor in opened and calls[descriptor] == OUTPUT_FSTAT_INTERRUPT_CALL:
            raise KeyboardInterrupt
        return real_fstat(descriptor)

    monkeypatch.setattr(stage_receipt.os, "open", record_open)
    monkeypatch.setattr(stage_receipt.os, "fstat", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        stage_receipt.publish(output_root, {"status": "PASS"})
    assert opened
    _assert_closed(opened[-1])


def test_fsync_parent_interrupt_never_closes_reused_foreign_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim_path = tmp_path / "victim"
    victim_path.write_text("alive", encoding="utf-8")
    victim: int | None = None

    def interrupt(descriptor: int) -> None:
        nonlocal victim
        victim = _reuse_descriptor(descriptor, victim_path)
        raise KeyboardInterrupt

    monkeypatch.setattr(release_io.os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        release_io._fsync_parent(tmp_path / "status.json")
    assert victim is not None
    assert os.read(victim, 5) == b"alive"
    os.close(victim)


def test_path_temporary_interrupt_never_closes_reused_foreign_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim_path = tmp_path / "victim"
    victim_path.write_text("alive", encoding="utf-8")
    victim: int | None = None

    def interrupt(descriptor: int, _content: bytes, *, name: str) -> None:
        nonlocal victim
        assert name == "path JSON"
        victim = _reuse_descriptor(descriptor, victim_path)
        raise KeyboardInterrupt

    monkeypatch.setattr(release_io, "_write_descriptor", interrupt)
    with pytest.raises(KeyboardInterrupt):
        release_io.write_json_atomic(tmp_path / "status.json", {"status": "EXECUTED"})
    assert victim is not None
    assert os.read(victim, 5) == b"alive"
    assert not list(tmp_path.glob(".status.json.*.tmp"))
    os.close(victim)


def test_frozen_file_interrupt_never_closes_reused_foreign_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "artifact.json").write_text("{}\n", encoding="utf-8")
    victim_path = tmp_path / "victim"
    victim_path.write_text("alive", encoding="utf-8")
    parent_fd = os.open(source, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    victim: int | None = None

    def interrupt(descriptor: int, **_kwargs: Any) -> None:
        nonlocal victim
        victim = _reuse_descriptor(descriptor, victim_path)
        raise KeyboardInterrupt

    monkeypatch.setattr(stage_io.package_root, "require_frozen_descriptor", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            stage_io._read_frozen_file(parent_fd, "artifact.json", relative="artifact.json")
    finally:
        os.close(parent_fd)
    assert victim is not None
    assert os.read(victim, 5) == b"alive"
    os.close(victim)


def test_frozen_tree_cleanup_never_closes_reused_directory_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    (source / "child").mkdir(parents=True)
    victim_path = tmp_path / "victim"
    victim_path.write_text("alive", encoding="utf-8")
    parent_fd = os.open(source, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    victim: int | None = None

    def interrupt(descriptor: int, **_kwargs: Any) -> None:
        nonlocal victim
        victim = _reuse_descriptor(descriptor, victim_path)
        raise KeyboardInterrupt

    monkeypatch.setattr(stage_io.package_root, "require_frozen_descriptor", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            stage_io._open_frozen_tree(parent_fd)
    finally:
        os.close(parent_fd)
    assert victim is not None
    assert os.read(victim, 5) == b"alive"
    os.close(victim)
