from __future__ import annotations

import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_official_package as package
from benchmarks import longmemeval_v2_release_official_publication as publication
from benchmarks import longmemeval_v2_release_package_archive as package_archive
from benchmarks import longmemeval_v2_release_package_object as package_object
from benchmarks import longmemeval_v2_release_package_process as process
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks.longmemeval_v2_release_handoff import ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import StagePlanError, load_json
from tools.tests.longmemeval_v2_release_package_support import (
    build_executed,
    stub_score_aware_boundary,
    successful_invoke,
    thaw_tree,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin immutable file flags",
)

FROZEN_DIRECTORY_MODE = 0o500
ROOT_READS_BEFORE_ATTACK = 2


@pytest.fixture(name="executed")
def _executed_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ExecutedStage:
    return build_executed(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _clear_outputs(tmp_path: Path) -> Any:
    yield
    thaw_tree(tmp_path)


def _package(
    executed: ExecutedStage,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    monkeypatch.setattr(process, "_invoke_command", successful_invoke)
    stub_score_aware_boundary(monkeypatch)
    return publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=root,
    )


def _cycle_file(path: Path, content: bytes) -> None:
    package_root.set_path_flags(path, 0)
    path.chmod(0o600)
    path.write_bytes(content)
    path.chmod(package_object.OBJECT_FILE_MODE)
    package_root.set_path_flags(path, package_root.IMMUTABLE_FLAG)


def test_darwin_immutable_parent_and_arms_deny_foreign_writes(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    result = _package(executed, packages_root, monkeypatch)
    authority_root = package_object.publication_path(packages_root, "aa-1-left").parent
    object_path = Path(result["package_object"]["path"])

    assert stat.S_IMODE(packages_root.stat().st_mode) == FROZEN_DIRECTORY_MODE
    assert package_root.file_flags(packages_root.stat()) & package_root.IMMUTABLE_FLAG
    arms_root = packages_root / "arms"
    assert stat.S_IMODE(arms_root.stat().st_mode) == FROZEN_DIRECTORY_MODE
    assert package_root.file_flags(arms_root.stat()) & package_root.IMMUTABLE_FLAG
    assert stat.S_IMODE(authority_root.stat().st_mode) == FROZEN_DIRECTORY_MODE
    assert package_root.file_flags(authority_root.stat()) & package_root.IMMUTABLE_FLAG
    assert package_root.file_flags(object_path.stat()) & package_root.IMMUTABLE_FLAG
    with pytest.raises(PermissionError):
        (packages_root / "foreign").mkdir()
    with pytest.raises(PermissionError):
        (arms_root / "foreign").mkdir()
    with pytest.raises(PermissionError):
        (authority_root / "foreign").mkdir()
    with pytest.raises(PermissionError):
        object_path.chmod(0o600)
    with pytest.raises(PermissionError):
        object_path.write_bytes(b"mutated")
    with pytest.raises(PermissionError):
        authority_root.rename(authority_root.with_name("moved"))


def test_late_mutable_arms_insertion_is_denied_during_consumer_validation(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    _package(executed, packages_root, monkeypatch)
    root_identity = (
        packages_root.stat().st_dev,
        packages_root.stat().st_ino,
    )
    real_read = package_tree._read_tree_at
    reads = 0
    denied = False

    def inject(
        directory_fd: int,
        *,
        prefix: str = "",
    ) -> tuple[dict[str, bytes], frozenset[str]]:
        nonlocal denied, reads
        result = real_read(directory_fd, prefix=prefix)
        current = os.fstat(directory_fd)
        if prefix == "" and (current.st_dev, current.st_ino) == root_identity:
            reads += 1
            if reads == ROOT_READS_BEFORE_ATTACK:
                try:
                    (packages_root / "arms" / "foreign-late").mkdir()
                except PermissionError:
                    denied = True
        return result

    monkeypatch.setattr(package_tree, "_read_tree_at", inject)
    result = publication.require_official_arm_package(
        executed,
        arm_id="aa-1-left",
        packages_root=packages_root,
    )
    assert result["status"] == "PASS"
    assert denied is True
    assert not (packages_root / "arms" / "foreign-late").exists()


def test_concurrent_sibling_publishers_serialize_without_losing_an_arm(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    right = deepcopy(executed.runs[0])
    right["arm_id"] = "aa-1-right"
    right["execution"]["run_id"] = "00000000-0000-0000-0000-000000000002"
    runs = (executed.runs[0], right)
    domains = executed.domains + tuple(
        replace(domain, arm_id="aa-1-right") for domain in executed.domains
    )
    paired = replace(executed, runs=runs, domains=domains)
    monkeypatch.setattr(package.state, "require_claimed_stage_plan", lambda _plan: list(runs))
    monkeypatch.setattr(process, "_invoke_command", successful_invoke)
    stub_score_aware_boundary(monkeypatch)

    def publish(arm_id: str) -> dict[str, Any]:
        return publication.package_official_arm(
            paired,
            arm_id=arm_id,
            packages_root=packages_root,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, ("aa-1-left", "aa-1-right")))
    assert [result["status"] for result in results] == ["PASS", "PASS"]
    assert {path.name for path in (packages_root / "arms").iterdir()} == {
        "aa-1-left",
        "aa-1-right",
    }


def test_publication_refreezes_arms_after_post_thaw_interrupt(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    real_thaw = package_root.thaw_arms_for_publication

    def thaw_then_interrupt(lease: package_root.PackageLease) -> None:
        real_thaw(lease)
        raise KeyboardInterrupt

    monkeypatch.setattr(package_root, "thaw_arms_for_publication", thaw_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _package(executed, packages_root, monkeypatch)
    arms_metadata = (packages_root / "arms").stat()
    assert stat.S_IMODE(arms_metadata.st_mode) == FROZEN_DIRECTORY_MODE
    assert package_root.file_flags(arms_metadata) & package_root.IMMUTABLE_FLAG


def test_publication_refreezes_arms_after_partial_thaw_interrupt(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    real_set_flags = package_root.release_io.set_fd_flags
    interrupted = False

    def clear_then_interrupt(descriptor: int, flags: int) -> None:
        nonlocal interrupted
        real_set_flags(descriptor, flags)
        if flags == 0 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(package_root.release_io, "set_fd_flags", clear_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _package(executed, packages_root, monkeypatch)
    arms_metadata = (packages_root / "arms").stat()
    assert interrupted is True
    assert stat.S_IMODE(arms_metadata.st_mode) == FROZEN_DIRECTORY_MODE
    assert package_root.file_flags(arms_metadata) & package_root.IMMUTABLE_FLAG


@pytest.mark.parametrize("target", ["authority", "object"])
def test_consumer_retains_child_descriptors_through_semantic_validation(
    target: str,
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    result = _package(executed, packages_root, monkeypatch)
    authority_path = package_object.publication_path(packages_root, "aa-1-left")
    target_path = (
        authority_path if target == "authority" else Path(result["package_object"]["path"])
    )
    real_require = package_archive.require_package_object

    def mutate_live_file(content: bytes) -> tuple[dict[str, bytes], dict[str, Any]]:
        validated = real_require(content)
        _cycle_file(target_path, b"{}\n" if target == "authority" else b"mutated\n")
        return validated

    monkeypatch.setattr(package_archive, "require_package_object", mutate_live_file)
    with pytest.raises(StagePlanError, match="changed during validation"):
        publication.require_official_arm_package(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )


@pytest.mark.parametrize("target", ["status", "paid", "source"])
def test_consumer_finishes_with_one_full_mutable_external_snapshot(
    target: str,
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    _package(executed, packages_root, monkeypatch)
    real_require = package_archive.require_package_object
    paths = {
        "status": Path(dict(executed.control_artifacts)["runner_status"]["path"]),
        "paid": Path(executed.domains[0].artifacts[0][1]["path"]),
        "source": Path(executed.plan["package_inputs"]["system_description"]["path"]),
    }

    def mutate_external(content: bytes) -> tuple[dict[str, bytes], dict[str, Any]]:
        validated = real_require(content)
        paths[target].write_text(f"mutated-{target}\n", encoding="utf-8")
        return validated

    monkeypatch.setattr(package_archive, "require_package_object", mutate_external)
    with pytest.raises(StagePlanError):
        publication.require_official_arm_package(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )


def test_builder_to_consumer_chmod_cycle_is_denied_by_immutable_object(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    monkeypatch.setattr(process, "_invoke_command", successful_invoke)
    stub_score_aware_boundary(monkeypatch)
    real_build = package.build_official_arm_publication
    denied = False

    def build_then_attack(*args: Any, **kwargs: Any) -> None:
        nonlocal denied
        real_build(*args, **kwargs)
        receipt = load_json(package_object.publication_path(packages_root, "aa-1-left"))
        try:
            Path(receipt["package_object"]["path"]).chmod(0o600)
        except PermissionError:
            denied = True

    monkeypatch.setattr(package, "build_official_arm_publication", build_then_attack)
    result = publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=packages_root,
    )
    assert denied is True
    assert result["status"] == "PASS"
