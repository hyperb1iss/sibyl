"""Retention checks exercise restore consumption and fail before overwriting evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from benchmarks.longmemeval_v2_release_package_archive import build_package_object
from tools.bench import evidence_bundle


def test_bundle_is_reproducible_and_restores_metric_inputs(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"rows": [{"correct": True}, {"correct": False}]}))
    corpus = tmp_path / "corpus.json"
    corpus.write_text('[{"session_id": "s1", "text": "historical evidence"}]')
    files = {"results/report.json": report, "inputs/corpus.json": corpus}
    bundle = evidence_bundle.preserve(files, tmp_path / "store")
    assert evidence_bundle.preserve(dict(reversed(list(files.items()))), bundle.parent) == bundle
    destination = tmp_path / "remote"
    names = evidence_bundle.restore(bundle, bundle.name.removesuffix(".tar.gz"), destination)
    assert names == sorted(files)
    for name, source in files.items():
        assert (destination / name).read_bytes() == source.read_bytes()
    restored = json.loads((destination / "results/report.json").read_text())
    expected_accuracy = 0.5
    assert (
        sum(row["correct"] for row in restored["rows"]) / len(restored["rows"]) == expected_accuracy
    )


def test_corruption_is_rejected_before_restore(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    bundle = evidence_bundle.preserve({"source": source}, tmp_path / "store")
    digest = bundle.name.removesuffix(".tar.gz")
    bundle.write_bytes(bundle.read_bytes() + b"modified")
    destination = tmp_path / "remote"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        evidence_bundle.restore(bundle, digest, destination)
    assert not destination.exists()
    with pytest.raises(ValueError, match="does not match"):
        evidence_bundle.preserve({"source": source}, bundle.parent)


def test_restore_never_overwrites_existing_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    bundle = evidence_bundle.preserve({"source": source}, tmp_path / "store")
    with pytest.raises(FileExistsError):
        evidence_bundle.restore(bundle, bundle.name.removesuffix(".tar.gz"), tmp_path)
    assert source.read_bytes() == b"evidence"


def test_collision_rejected_before_creating_destination(tmp_path: Path) -> None:
    content, _ = build_package_object({"inputs": b"file", "inputs/data": b"nested"})
    bundle = tmp_path / "bad.tar.gz"
    bundle.write_bytes(content)
    destination = tmp_path / "remote"
    with pytest.raises(ValueError, match="collision"):
        evidence_bundle.restore(bundle, hashlib.sha256(content).hexdigest(), destination)
    assert not destination.exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "x/../../outside"])
def test_unsafe_archive_names_rejected(tmp_path: Path, name: str) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    with pytest.raises(ValueError, match="unsafe"):
        evidence_bundle.preserve({name: source}, tmp_path / "store")
    assert not (tmp_path / "store").exists()


def test_symlink_inputs_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="regular file"):
        evidence_bundle.preserve({"source": link}, tmp_path / "store")


def test_cli_rejects_duplicate_inventory_names(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        evidence_bundle.main(
            [
                "preserve",
                "--store",
                str(tmp_path / "store"),
                "--file",
                "report=a",
                "--file",
                "report=b",
            ]
        )
    assert exc.value.code == 1
    assert not (tmp_path / "store").exists()


def test_failed_restore_removes_partial_output_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"evidence")
    bundle = evidence_bundle.preserve({"a": source, "b": source}, tmp_path / "store")
    digest = bundle.name.removesuffix(".tar.gz")
    destination = tmp_path / "remote"
    original_chmod = evidence_bundle.os.chmod

    def fail_second_member(path: Path, mode: int) -> None:
        if path == destination / "b":
            raise OSError("simulated disk failure")
        original_chmod(path, mode)

    with monkeypatch.context() as patch:
        patch.setattr(evidence_bundle.os, "chmod", fail_second_member)
        with pytest.raises(OSError, match="simulated disk failure"):
            evidence_bundle.restore(bundle, digest, destination)
    assert not destination.exists()
    assert evidence_bundle.restore(bundle, digest, destination) == ["a", "b"]
