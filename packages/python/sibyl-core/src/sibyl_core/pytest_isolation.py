"""Give concurrent Moon pytest processes independent temporary roots."""

from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

_CREATED_BASETEMPS: set[Path] = set()
_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _target_slug(target: str | None) -> str:
    slug = _UNSAFE_COMPONENT.sub("-", target or "pytest").strip("-.")
    return slug or "pytest"


def _new_basetemp(target: str | None) -> Path:
    temp_root = Path(tempfile.gettempdir()) / "sibyl-pytest"
    temp_root.mkdir(parents=True, exist_ok=True)
    basetemp = Path(tempfile.mkdtemp(prefix=f"{_target_slug(target)}-", dir=temp_root))
    _CREATED_BASETEMPS.add(basetemp)
    return basetemp


def _cleanup_created_basetemps() -> None:
    for basetemp in tuple(_CREATED_BASETEMPS):
        shutil.rmtree(basetemp, ignore_errors=True)
        _CREATED_BASETEMPS.discard(basetemp)


def pytest_configure(config: pytest.Config) -> None:
    """Select an isolated root unless the caller already chose one."""
    if config.option.basetemp is None:
        config.option.basetemp = _new_basetemp(os.getenv("MOON_TARGET"))


atexit.register(_cleanup_created_basetemps)
