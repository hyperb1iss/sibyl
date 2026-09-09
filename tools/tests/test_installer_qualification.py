"""Reject mixed unpublished releases before starting the private index."""

from pathlib import Path
from zipfile import ZipFile

import pytest
from tools.installer.qualify import wheel_release


def wheels(root: Path, versions: tuple[str, str, str]) -> list[Path]:
    paths = []
    for name, version in zip(("sibyl_core", "sibyl_dev", "sibyld"), versions, strict=True):
        path = root / f"{name}-{version}-py3-none-any.whl"
        with ZipFile(path, "w") as archive:
            archive.writestr(
                f"{name}-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            )
        paths.append(path)
    return paths


def test_release_uses_wheel_metadata(tmp_path):
    assert wheel_release(wheels(tmp_path, ("1.4.0", "1.4.0", "1.4.0"))) == "1.4.0"


def test_mixed_releases_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="releases must agree"):
        wheel_release(wheels(tmp_path, ("1.4.0", "1.3.2", "1.4.0")))


def test_missing_and_duplicate_project_wheels_rejected(tmp_path):
    artifacts = wheels(tmp_path, ("1.4.0", "1.4.0", "1.4.0"))
    with pytest.raises(RuntimeError, match="releases must agree"):
        wheel_release(artifacts[:2])
    with pytest.raises(RuntimeError, match="duplicate"):
        wheel_release([*artifacts, artifacts[0]])
