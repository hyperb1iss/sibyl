from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest

from sibyl_core import pytest_isolation


class _Option:
    def __init__(self, basetemp: Path | None = None) -> None:
        self.basetemp = basetemp


class _Config:
    def __init__(self, basetemp: Path | None = None) -> None:
        self.option = _Option(basetemp)


def _pytest_config(basetemp: Path | None = None) -> tuple[pytest.Config, _Config]:
    config = _Config(basetemp)
    return cast(pytest.Config, config), config


def test_moon_processes_get_unique_os_temp_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pytest_isolation.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("MOON_TARGET", "cli:test")
    first_pytest_config, first = _pytest_config()
    second_pytest_config, second = _pytest_config()

    pytest_isolation.pytest_configure(first_pytest_config)
    pytest_isolation.pytest_configure(second_pytest_config)

    assert first.option.basetemp is not None
    assert second.option.basetemp is not None
    assert first.option.basetemp != second.option.basetemp
    assert first.option.basetemp.parent == tmp_path / "sibyl-pytest"
    assert second.option.basetemp.parent == tmp_path / "sibyl-pytest"
    assert first.option.basetemp.name.startswith("cli-test-")

    for basetemp in (first.option.basetemp, second.option.basetemp):
        shutil.rmtree(basetemp)
        pytest_isolation._CREATED_BASETEMPS.discard(basetemp)
        assert not basetemp.exists()


def test_explicit_basetemp_remains_caller_owned(tmp_path: Path) -> None:
    caller_basetemp = tmp_path / "caller-owned"
    pytest_config, config = _pytest_config(caller_basetemp)

    pytest_isolation.pytest_configure(pytest_config)

    assert config.option.basetemp == caller_basetemp


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("api:test", "api-test"),
        ("project/task name", "project-task-name"),
        ("...", "pytest"),
        (None, "pytest"),
    ],
)
def test_target_slug_is_path_safe(target: str | None, expected: str) -> None:
    assert pytest_isolation._target_slug(target) == expected
