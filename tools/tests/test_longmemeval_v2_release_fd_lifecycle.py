from __future__ import annotations

import os
import signal
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_plan_publication as plan_publication
from benchmarks import longmemeval_v2_release_plan_safety as plan_safety
from benchmarks import longmemeval_v2_release_stage_io as stage_io
from benchmarks import longmemeval_v2_release_stage_receipt as stage_receipt
from benchmarks import longmemeval_v2_release_stage_transaction as stage_transaction

OUTPUT_FSTAT_INTERRUPT_CALL = 2
POST_WRITE_VALIDATION_CALL = 2
FINAL_PATH_VALIDATION_CALL = 3
PLAN_WRITER_COUNT = 8

requires_darwin_file_flags = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin immutable file flags",
)


@pytest.fixture(autouse=True)
def _clear_immutable_plan_files(tmp_path: Path) -> Any:
    yield
    for current, _directories, files in os.walk(tmp_path, topdown=False):
        for name in files:
            with suppress(OSError):
                package_root.set_path_flags(Path(current) / name, 0)
        with suppress(OSError):
            package_root.set_path_flags(current, 0)
            os.chmod(current, 0o700)


def test_owned_plan_publication_rejects_non_darwin_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plan_publication.sys, "platform", "linux")

    with pytest.raises(OSError, match="requires macOS"):
        plan_publication._write_json_once_rename_atomic_at(
            -1,
            "plan.json",
            {},
            authority_holder=plan_safety.OwnedPlanFileHolder(),
        )


def _assert_closed(descriptor: int) -> None:
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def _reuse_descriptor(descriptor: int, victim_path: Path) -> int:
    os.close(descriptor)
    source = os.open(victim_path, os.O_RDONLY)
    if source == descriptor:
        return source
    os.dup2(source, descriptor)
    os.close(source)
    return descriptor


@requires_darwin_file_flags
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


@requires_darwin_file_flags
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


@requires_darwin_file_flags
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


@requires_darwin_file_flags
def test_owned_plan_publication_never_redirects_after_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = tmp_path / "plans"
    redirected = tmp_path / "redirected"
    displaced = tmp_path / "displaced"
    redirected.mkdir()
    real_write = plan_publication._write_json_once_rename_atomic_at

    def swap_then_write(
        directory_fd: int,
        name: str,
        payload: dict[str, Any],
        *,
        authority_holder: plan_safety.OwnedPlanFileHolder,
    ) -> None:
        plans.rename(displaced)
        plans.symlink_to(redirected, target_is_directory=True)
        real_write(
            directory_fd,
            name,
            payload,
            authority_holder=authority_holder,
        )

    monkeypatch.setattr(
        plan_publication,
        "_write_json_once_rename_atomic_at",
        swap_then_write,
    )

    with pytest.raises(OSError, match="location changed"):
        plan_publication.write_json_once_owned_path(
            plans / "stage.json",
            {"status": "sealed"},
        )
    assert not (redirected / "stage.json").exists()
    assert not (displaced / "stage.json").exists()


def test_owned_plan_publication_cleanup_preserves_a_reused_foreign_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    victim_path = tmp_path / "victim"
    victim_path.write_text("alive", encoding="utf-8")
    real_require = plan_publication._require_owned_path_directories
    calls = 0
    victim: int | None = None

    def reuse_after_validation(directories: list[Any]) -> None:
        nonlocal calls, victim
        calls += 1
        real_require(directories)
        if calls == POST_WRITE_VALIDATION_CALL:
            victim = _reuse_descriptor(directories[-1].descriptor, victim_path)
            raise KeyboardInterrupt

    monkeypatch.setattr(
        plan_publication,
        "_require_owned_path_directories",
        reuse_after_validation,
    )

    with pytest.raises(KeyboardInterrupt):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert victim is not None
    assert os.read(victim, 5) == b"alive"
    os.close(victim)


def test_owned_plan_publication_interrupt_removes_the_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_write = plan_publication._write_descriptor

    def interrupt(descriptor: int, content: bytes) -> None:
        real_write(descriptor, content)
        raise KeyboardInterrupt

    monkeypatch.setattr(plan_publication, "_write_descriptor", interrupt)

    with pytest.raises(KeyboardInterrupt):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert not target.exists()
    assert not list(target.parent.glob(".stage.json.*.tmp"))


@requires_darwin_file_flags
def test_owned_plan_publication_rejects_final_target_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_require = plan_publication._require_owned_path_directories
    calls = 0

    def replace_before_final_validation(directories: list[Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == FINAL_PATH_VALIDATION_CALL:
            replacement = target.with_name("replacement.json")
            replacement.write_text("{}", encoding="utf-8")
            os.replace(replacement, target)
        real_require(directories)

    monkeypatch.setattr(
        plan_publication,
        "_require_owned_path_directories",
        replace_before_final_validation,
    )
    with pytest.raises(OSError, match="Operation not permitted"):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert not target.exists()


@requires_darwin_file_flags
def test_owned_plan_publication_rejects_final_target_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_require = plan_publication._require_owned_path_directories
    calls = 0

    def chmod_before_final_validation(directories: list[Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == FINAL_PATH_VALIDATION_CALL:
            target.chmod(0o600)
        real_require(directories)

    monkeypatch.setattr(
        plan_publication,
        "_require_owned_path_directories",
        chmod_before_final_validation,
    )
    with pytest.raises(OSError, match="Operation not permitted"):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})


@requires_darwin_file_flags
def test_owned_plan_publication_rejects_in_place_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_require = plan_publication._require_owned_path_directories
    calls = 0

    def mutate_before_final_validation(directories: list[Any]) -> None:
        nonlocal calls
        calls += 1
        if calls == FINAL_PATH_VALIDATION_CALL:
            target.chmod(0o600)
            target.write_text("{}", encoding="utf-8")
            target.chmod(0o400)
        real_require(directories)

    monkeypatch.setattr(
        plan_publication,
        "_require_owned_path_directories",
        mutate_before_final_validation,
    )
    with pytest.raises(OSError, match="Operation not permitted"):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})


@requires_darwin_file_flags
def test_owned_plan_publication_preserves_reused_plan_file_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    victim_path = tmp_path / "victim"
    victim_path.write_text("alive", encoding="utf-8")
    real_require = plan_publication._require_owned_plan_file
    calls = 0
    victim: int | None = None

    def reuse_before_final_validation(
        authority: plan_safety.OwnedPlanFile,
        directory_fd: int,
        name: str,
        *,
        expected: bytes,
        expected_path: Path | None = None,
    ) -> None:
        nonlocal calls, victim
        calls += 1
        if calls == POST_WRITE_VALIDATION_CALL:
            victim = _reuse_descriptor(authority.descriptor, victim_path)
        real_require(
            authority,
            directory_fd,
            name,
            expected=expected,
            expected_path=expected_path,
        )

    monkeypatch.setattr(
        plan_publication,
        "_require_owned_plan_file",
        reuse_before_final_validation,
    )
    with pytest.raises(OSError, match="unsafe type or mode"):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert victim is not None
    assert os.read(victim, 5) == b"alive"
    os.close(victim)


@requires_darwin_file_flags
def test_owned_plan_publication_rejects_late_parent_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = tmp_path / "plans"
    displaced = tmp_path / "displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    target = plans / "stage.json"
    real_require = plan_publication._require_owned_plan_file
    calls = 0

    def relocate_before_final_file_check(
        authority: plan_safety.OwnedPlanFile,
        directory_fd: int,
        name: str,
        *,
        expected: bytes,
        expected_path: Path | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == POST_WRITE_VALIDATION_CALL:
            plans.rename(displaced)
            plans.symlink_to(replacement, target_is_directory=True)
        real_require(
            authority,
            directory_fd,
            name,
            expected=expected,
            expected_path=expected_path,
        )

    monkeypatch.setattr(
        plan_publication,
        "_require_owned_plan_file",
        relocate_before_final_file_check,
    )
    with pytest.raises(OSError, match="Operation not permitted"):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert not target.exists()
    assert not (displaced / "stage.json").exists()


@requires_darwin_file_flags
def test_owned_plan_publication_rejects_relocation_after_final_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = tmp_path / "plans"
    displaced = tmp_path / "displaced"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    target = plans / "stage.json"
    real_require = plan_publication._require_owned_plan_file
    calls = 0

    def relocate_after_final_file_check(
        authority: plan_safety.OwnedPlanFile,
        directory_fd: int,
        name: str,
        *,
        expected: bytes,
        expected_path: Path | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        real_require(
            authority,
            directory_fd,
            name,
            expected=expected,
            expected_path=expected_path,
        )
        if calls == FINAL_PATH_VALIDATION_CALL:
            plans.rename(displaced)
            plans.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(
        plan_publication,
        "_require_owned_plan_file",
        relocate_after_final_file_check,
    )
    with pytest.raises(OSError, match="Operation not permitted"):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert not target.exists()
    assert not (displaced / "stage.json").exists()


@pytest.mark.parametrize("acquisition", ["root", "child", "temporary", "final"])
def test_owned_plan_publication_defers_real_sigint_until_fd_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    acquisition: str,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_open = plan_publication.os.open
    send_signal = Event()
    signal_sent = Event()
    opened: list[int] = []

    def interrupt_process() -> None:
        assert send_signal.wait(timeout=5)
        os.kill(os.getpid(), signal.SIGINT)
        signal_sent.set()

    def open_then_request_sigint(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        path_text = os.fspath(path)
        selected = (
            (acquisition == "root" and path_text == target.anchor)
            or (acquisition == "child" and path_text == target.parent.name)
            or (acquisition == "temporary" and path_text.startswith(".stage.json."))
            or (acquisition == "final" and path_text == target.name)
        )
        if selected and not opened:
            opened.append(descriptor)
            send_signal.set()
            assert signal_sent.wait(timeout=5)
        return descriptor

    sender = Thread(target=interrupt_process)
    sender.start()
    monkeypatch.setattr(plan_publication.os, "open", open_then_request_sigint)
    try:
        with pytest.raises(KeyboardInterrupt):
            plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    finally:
        sender.join(timeout=5)
    assert not sender.is_alive()
    assert opened
    _assert_closed(opened[0])
    assert not target.exists()
    assert not target.parent.exists()


@requires_darwin_file_flags
def test_owned_plan_publication_has_one_real_darwin_winner(
    tmp_path: Path,
) -> None:
    target = tmp_path / "plans" / "stage.json"

    def publish(_index: int) -> str:
        try:
            plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
        except (FileExistsError, OSError):
            return "rejected"
        return "published"

    with ThreadPoolExecutor(max_workers=PLAN_WRITER_COUNT) as pool:
        results = list(pool.map(publish, range(PLAN_WRITER_COUNT)))
    assert results.count("published") == 1
    assert results.count("rejected") == PLAN_WRITER_COUNT - 1
    metadata = target.stat()
    assert stat.S_IMODE(metadata.st_mode) == plan_publication.PLAN_FILE_MODE
    assert package_root.file_flags(metadata) == plan_publication.PLAN_FILE_FLAGS
    assert not list(target.parent.glob(".stage.json.*.tmp"))


@requires_darwin_file_flags
def test_owned_plan_publication_recovers_a_post_freeze_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_fsync = plan_publication.os.fsync
    interrupted = False

    def interrupt_after_freeze(descriptor: int) -> None:
        nonlocal interrupted
        metadata = plan_publication.os.fstat(descriptor)
        if not interrupted and getattr(metadata, "st_flags", 0) == plan_publication.PLAN_FILE_FLAGS:
            interrupted = True
            raise KeyboardInterrupt
        real_fsync(descriptor)

    monkeypatch.setattr(plan_publication.os, "fsync", interrupt_after_freeze)
    with pytest.raises(KeyboardInterrupt):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert interrupted
    assert not target.exists()
    assert not list(target.parent.glob(".stage.json.*.tmp"))
    monkeypatch.setattr(plan_publication.os, "fsync", real_fsync)
    plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert package_root.file_flags(target.stat()) == plan_publication.PLAN_FILE_FLAGS


@requires_darwin_file_flags
def test_owned_plan_publication_recovers_post_rename_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_rename = plan_publication.release_io.rename_once_atomic_at

    def interrupt_after_rename(*args: Any) -> None:
        real_rename(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        plan_publication.release_io,
        "rename_once_atomic_at",
        interrupt_after_rename,
    )
    with pytest.raises(KeyboardInterrupt):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert not target.exists()
    assert not list(target.parent.glob(".stage.json.*.tmp"))
    monkeypatch.setattr(plan_publication.release_io, "rename_once_atomic_at", real_rename)
    plan_publication.write_json_once_owned_path(target, {"status": "sealed"})


def test_owned_plan_identity_capture_closes_on_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_fstat = release_io.os.fstat
    final_descriptor: int | None = None

    def interrupt_during_capture(descriptor: int) -> os.stat_result:
        nonlocal final_descriptor
        final_descriptor = descriptor
        raise KeyboardInterrupt

    monkeypatch.setattr(release_io.os, "fstat", interrupt_during_capture)
    with pytest.raises(KeyboardInterrupt):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    monkeypatch.setattr(release_io.os, "fstat", real_fstat)
    assert final_descriptor is not None
    _assert_closed(final_descriptor)
    assert not target.exists()


def test_owned_plan_identity_capture_does_not_close_a_reused_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    victim_path = tmp_path / "victim"
    victim_path.write_text("alive", encoding="utf-8")
    victim: int | None = None

    def fail_after_helper_cleanup(descriptor: int) -> release_io.DescriptorIdentity:
        nonlocal victim
        victim = _reuse_descriptor(descriptor, victim_path)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        release_io,
        "capture_descriptor_identity",
        fail_after_helper_cleanup,
    )
    with pytest.raises(KeyboardInterrupt):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert victim is not None
    assert os.read(victim, 5) == b"alive"
    os.close(victim)
    assert not target.exists()


@requires_darwin_file_flags
def test_owned_plan_helper_registers_authority_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "plans" / "stage.json"
    real_open = plan_publication._open_owned_plan_file
    final_descriptor: int | None = None

    def interrupt_after_open(
        directory_fd: int,
        name: str,
        *,
        expected: bytes,
        renamed_identity: release_io.DescriptorIdentity,
        holder: plan_safety.OwnedPlanFileHolder,
    ) -> None:
        nonlocal final_descriptor
        real_open(
            directory_fd,
            name,
            expected=expected,
            renamed_identity=renamed_identity,
            holder=holder,
        )
        final_descriptor = holder.descriptor
        raise KeyboardInterrupt

    monkeypatch.setattr(plan_publication, "_open_owned_plan_file", interrupt_after_open)
    with pytest.raises(KeyboardInterrupt):
        plan_publication.write_json_once_owned_path(target, {"status": "sealed"})
    assert final_descriptor is not None
    _assert_closed(final_descriptor)
    assert not target.exists()
