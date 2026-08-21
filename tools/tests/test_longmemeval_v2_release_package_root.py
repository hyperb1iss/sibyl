from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_command as command_runner
from benchmarks import longmemeval_v2_release_package_policy as package_policy
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks.longmemeval_v2_release_inputs import StagePlanError

ARM_ID = "aa-1-left"
RUNS = ({"arm_id": ARM_ID},)
OWNED_DESCRIPTOR_COUNT = 2
LEASE_DESCRIPTOR_COUNT = 7


def _plan(tmp_path: Path) -> dict[str, Any]:
    roots = {
        name: tmp_path / name for name in ("paid", "official", "data", "package-inputs", "upstream")
    }
    for root in roots.values():
        root.mkdir()
    system = roots["package-inputs"] / "SYSTEM_DESCRIPTION.md"
    adapter = roots["package-inputs"] / "sibyl_memory.py"
    authorization = roots["upstream"] / "authorization.json"
    for artifact in (system, adapter, authorization):
        artifact.write_text("sealed\n", encoding="utf-8")
    return {
        "output_root": str(roots["paid"]),
        "official_source": {"path": str(roots["official"])},
        "dataset": {"root": str(roots["data"])},
        "package_inputs": {
            "system_description": {"path": str(system)},
            "adapter": {"path": str(adapter)},
        },
        "memory_bindings": {},
        "upstream_bindings": {},
        "spec": {
            "upstream": {
                "aa_authorization": str(authorization),
                "preregistration_authorization": None,
            }
        },
    }


@pytest.mark.parametrize(
    "sealed_kind",
    [
        "dataset",
        "official-source",
        "memory",
        "package-input",
        "upstream-authorization",
        "source-checkout",
    ],
)
def test_packages_root_is_disjoint_from_every_sealed_input(
    sealed_kind: str,
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    if sealed_kind == "dataset":
        sealed_root = Path(plan["dataset"]["root"])
        packages_root = sealed_root / "packages"
    elif sealed_kind == "official-source":
        sealed_root = Path(plan["official_source"]["path"])
        packages_root = sealed_root / "packages"
    elif sealed_kind == "memory":
        sealed_root = tmp_path / "saved-memory"
        sealed_root.mkdir()
        plan["memory_bindings"] = {"baseline": {"web": {"path": str(sealed_root)}}}
        packages_root = sealed_root / "packages"
    elif sealed_kind == "package-input":
        packages_root = Path(plan["package_inputs"]["adapter"]["path"]).parent
    elif sealed_kind == "upstream-authorization":
        packages_root = Path(plan["spec"]["upstream"]["aa_authorization"]).parent
    else:
        packages_root = package_policy.SIBYL_ROOT
    if not packages_root.exists():
        packages_root.mkdir()

    with pytest.raises(StagePlanError, match="overlaps sealed execution inputs"):
        package_root.bind_packages_root(plan, RUNS, packages_root)


def test_packages_root_rejects_a_case_alias_under_a_sealed_root(tmp_path: Path) -> None:
    protected = tmp_path / "Protected"
    packages = protected / "packages"
    packages.mkdir(parents=True)
    alias = protected.with_name("protected") / "packages"
    if not alias.exists() or not alias.samefile(packages):
        pytest.skip("requires a case-insensitive filesystem")
    plan = {"output_root": str(protected)}

    with pytest.raises(StagePlanError, match="overlaps sealed execution inputs"):
        package_root.bind_packages_root(plan, RUNS, alias)


def test_fd_owned_child_writes_file_and_tar_without_lexical_redirection(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages_root)
    lease = package_root.create_arm_lease(parent, ARM_ID)
    try:
        (lease.arm.path / "source.txt").write_text("proof\n", encoding="utf-8")
        tar = shutil.which("tar")
        assert tar is not None
        commands = (
            (
                "file",
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('proof.txt').write_text('owned')",
                ],
            ),
            ("tar", [tar, "-czf", "proof.tar.gz", "source.txt"]),
        )
        for name, command in commands:
            assert (
                command_runner.invoke_command(
                    command,
                    log_path=lease.arm.path / f"logs/{name}.jsonl",
                    secrets=(),
                    working_directory_fd=lease.arm_fd,
                    working_directory_identity=(lease.arm.device, lease.arm.inode),
                    working_directory_path=lease.arm.path,
                    log_directory_fd=lease.logs_fd,
                    log_directory_identity=(lease.logs.device, lease.logs.inode),
                    log_name=f"{name}.jsonl",
                )
                == 0
            )
        assert (lease.arm.path / "proof.txt").read_text(encoding="utf-8") == "owned"
        assert (lease.arm.path / "proof.tar.gz").stat().st_size > 0

        displaced = tmp_path / "displaced"
        os.chflags(packages_root, 0)
        packages_root.chmod(0o700)
        packages_root.rename(displaced)
        packages_root.mkdir()
        replacement_arm = packages_root / ARM_ID
        replacement_arm.mkdir()
        redirected = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('after-swap.txt').write_text('owned')",
        ]
        assert (
            command_runner.invoke_command(
                redirected,
                log_path=lease.arm.path / "logs/after-swap.jsonl",
                secrets=(),
                working_directory_fd=lease.arm_fd,
                working_directory_identity=(lease.arm.device, lease.arm.inode),
                working_directory_path=lease.arm.path,
                log_directory_fd=lease.logs_fd,
                log_directory_identity=(lease.logs.device, lease.logs.inode),
                log_name="after-swap.jsonl",
            )
            == 0
        )
        assert (lease.arm.path / "after-swap.txt").read_text(encoding="utf-8") == "owned"
        assert list(replacement_arm.iterdir()) == []
        with pytest.raises(StagePlanError, match="filesystem identity changed"):
            package_root.require_package_roots(lease.parent, lease.arm)
    finally:
        lease.close()


def test_sandbox_denies_descendant_symlink_escape(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    escape = tmp_path / "escape"
    escape.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    lease = package_root.create_arm_lease(parent, ARM_ID)
    try:
        (lease.arm.path / "submission").symlink_to(escape, target_is_directory=True)
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('submission/escaped.txt').write_text('bad')",
        ]
        assert (
            command_runner.invoke_command(
                command,
                log_path=lease.arm.path / "logs/symlink.jsonl",
                secrets=(),
                working_directory_fd=lease.arm_fd,
                working_directory_identity=(lease.arm.device, lease.arm.inode),
                working_directory_path=lease.arm.path,
                log_directory_fd=lease.logs_fd,
                log_directory_identity=(lease.logs.device, lease.logs.inode),
                log_name="symlink.jsonl",
            )
            != 0
        )
        assert not (escape / "escaped.txt").exists()
        with pytest.raises(StagePlanError, match="symlink"):
            package_tree.read_owned_tree(lease)
    finally:
        lease.close()


def test_reused_package_descriptor_is_rejected_before_use(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    escape = tmp_path / "escape"
    escape.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    lease = package_root.create_arm_lease(parent, ARM_ID)
    os.close(lease.arm_fd)
    escape_fd = os.open(escape, os.O_RDONLY)
    if escape_fd != lease.arm_fd:
        os.dup2(escape_fd, lease.arm_fd)
        os.close(escape_fd)
    with pytest.raises(StagePlanError, match="changed before descriptor ownership"):
        package_root.require_lease(lease)
    with pytest.raises(StagePlanError, match="descriptor ownership changed"):
        lease.close()
    os.fstat(lease.arm_fd)
    os.close(lease.arm_fd)


def test_package_lease_construction_closes_partial_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    real_open_owner = package_root._open_owned_directory
    real_open_child = package_root._open_child_directory
    opened: list[int] = []

    def capture_parent(owner: package_root.OwnedDirectory, *, name: str) -> int:
        descriptor = real_open_owner(owner, name=name)
        opened.append(descriptor)
        return descriptor

    def fail_logs(parent_fd: int, name: str) -> int:
        if name == "logs":
            raise StagePlanError("simulated log open failure")
        descriptor = real_open_child(parent_fd, name)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(package_root, "_open_owned_directory", capture_parent)
    monkeypatch.setattr(package_root, "_open_child_directory", fail_logs)
    with pytest.raises(StagePlanError, match="log open failure"):
        package_root.create_arm_lease(parent, ARM_ID)
    for descriptor in opened:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


def test_command_interrupt_closes_owned_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    lease = package_root.create_arm_lease(parent, ARM_ID)
    real_duplicate = command_runner._owned_duplicate
    duplicates: list[int] = []

    def capture_duplicate(
        descriptor: int,
        identity: tuple[int, int],
        *,
        name: str,
    ) -> int:
        duplicate = real_duplicate(descriptor, identity, name=name)
        duplicates.append(duplicate)
        return duplicate

    def interrupt(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(command_runner, "_owned_duplicate", capture_duplicate)
    monkeypatch.setattr(command_runner.subprocess, "Popen", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            command_runner.invoke_command(
                [sys.executable, "-c", "pass"],
                log_path=lease.arm.path / "logs/interrupt.jsonl",
                secrets=(),
                working_directory_fd=lease.arm_fd,
                working_directory_identity=(lease.arm.device, lease.arm.inode),
                working_directory_path=lease.arm.path,
                log_directory_fd=lease.logs_fd,
                log_directory_identity=(lease.logs.device, lease.logs.inode),
                log_name="interrupt.jsonl",
            )
        assert len(duplicates) == OWNED_DESCRIPTOR_COUNT
        for descriptor in duplicates:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(descriptor)
    finally:
        lease.close()


@pytest.mark.parametrize("failure", [StagePlanError("owner race"), KeyboardInterrupt()])
def test_arm_lease_final_owner_failure_closes_every_descriptor(
    failure: BaseException,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    real_open_owner = package_root._open_owned_directory
    real_open_child = package_root._open_child_directory
    opened: list[int] = []

    def capture_parent(owner: package_root.OwnedDirectory, *, name: str) -> int:
        descriptor = real_open_owner(owner, name=name)
        opened.append(descriptor)
        return descriptor

    def capture_child(parent_fd: int, name: str) -> int:
        descriptor = real_open_child(parent_fd, name)
        opened.append(descriptor)
        return descriptor

    def fail_final_owner(
        parent_owner: package_root.OwnedDirectory,
        arm_owner: package_root.OwnedDirectory,
    ) -> None:
        del parent_owner, arm_owner
        raise failure

    monkeypatch.setattr(package_root, "_open_owned_directory", capture_parent)
    monkeypatch.setattr(package_root, "_open_child_directory", capture_child)
    monkeypatch.setattr(package_root, "require_package_roots", fail_final_owner)
    with pytest.raises(type(failure)):
        package_root.create_arm_lease(parent, ARM_ID)
    assert len(opened) == LEASE_DESCRIPTOR_COUNT
    for descriptor in opened:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


def test_second_descriptor_duplication_interrupt_closes_first_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    lease = package_root.create_arm_lease(parent, ARM_ID)
    real_duplicate = command_runner._owned_duplicate
    duplicate: int | None = None
    calls = 0

    def interrupt_second(
        descriptor: int,
        identity: tuple[int, int],
        *,
        name: str,
    ) -> int:
        nonlocal calls, duplicate
        calls += 1
        if calls == OWNED_DESCRIPTOR_COUNT:
            raise KeyboardInterrupt
        duplicate = real_duplicate(descriptor, identity, name=name)
        return duplicate

    monkeypatch.setattr(command_runner, "_owned_duplicate", interrupt_second)
    try:
        with pytest.raises(KeyboardInterrupt):
            command_runner.invoke_command(
                [sys.executable, "-c", "pass"],
                log_path=lease.arm.path / "logs/interrupt-second.jsonl",
                secrets=(),
                working_directory_fd=lease.arm_fd,
                working_directory_identity=(lease.arm.device, lease.arm.inode),
                working_directory_path=lease.arm.path,
                log_directory_fd=lease.logs_fd,
                log_directory_identity=(lease.logs.device, lease.logs.inode),
                log_name="interrupt-second.jsonl",
            )
        assert duplicate is not None
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(duplicate)
    finally:
        lease.close()


def test_owned_tree_rejects_foreign_empty_directory(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    lease = package_root.create_arm_lease(parent, ARM_ID)
    try:
        (lease.logs.path / "complete.jsonl").write_text("{}\n", encoding="utf-8")
        os.mkdir("foreign-empty", dir_fd=lease.arm_fd)
        with pytest.raises(StagePlanError, match="directory inventory"):
            package_tree.read_owned_tree(lease)
    finally:
        lease.close()


def test_receipt_owner_failure_closes_unassigned_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    lease = package_root.create_arm_lease(parent, ARM_ID)
    real_identity = package_root._directory_identity
    real_open_child = package_root._open_child_directory
    receipt_fd: int | None = None

    def fail_receipt_identity(path: Path, *, name: str) -> package_root.OwnedDirectory:
        if name == "official command receipts":
            raise StagePlanError("simulated receipt identity race")
        return real_identity(path, name=name)

    def capture_receipt(parent_fd: int, name: str) -> int:
        nonlocal receipt_fd
        descriptor = real_open_child(parent_fd, name)
        if name == "command_receipts":
            receipt_fd = descriptor
        return descriptor

    monkeypatch.setattr(package_root, "_directory_identity", fail_receipt_identity)
    monkeypatch.setattr(package_root, "_open_child_directory", capture_receipt)
    try:
        with pytest.raises(StagePlanError, match="receipt identity race"):
            package_root.require_receipts_fd(lease)
        assert receipt_fd is not None
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(receipt_fd)
        assert lease.receipts_fd is None
        assert lease.receipts is None
    finally:
        lease.close()


def test_open_owned_child_interrupt_closes_new_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    packages = tmp_path / "packages"
    packages.mkdir()
    parent = package_root.bind_packages_root(plan, RUNS, packages)
    parent_fd = package_root.open_owned_directory(parent, name="packages")
    real_require_identity = package_root._require_descriptor_identity
    real_open_child = package_root._open_child_directory
    child_fd: int | None = None
    calls = 0

    def capture_child(directory_fd: int, name: str) -> int:
        nonlocal child_fd
        child_fd = real_open_child(directory_fd, name)
        return child_fd

    def interrupt_after_open(
        descriptor: int,
        owner: package_root.OwnedDirectory,
        *,
        name: str,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == OWNED_DESCRIPTOR_COUNT:
            raise KeyboardInterrupt
        real_require_identity(descriptor, owner, name=name)

    monkeypatch.setattr(package_root, "_open_child_directory", capture_child)
    monkeypatch.setattr(package_root, "_require_descriptor_identity", interrupt_after_open)
    try:
        with pytest.raises(KeyboardInterrupt):
            package_root.open_owned_child(parent, parent_fd, "objects", create=True)
        assert child_fd is not None
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(child_fd)
    finally:
        os.close(parent_fd)
