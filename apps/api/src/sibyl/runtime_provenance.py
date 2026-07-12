"""Runtime build provenance captured when the API starts."""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _git_output(*args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [git, *args],
            cwd=_REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _optional_bool(value: str | None) -> bool | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return None


@lru_cache(maxsize=1)
def get_runtime_provenance() -> dict[str, Any]:
    configured_commit = os.getenv("SIBYL_GIT_COMMIT", "").strip()
    git_commit = _git_output("rev-parse", "HEAD") if not configured_commit else None
    commit = configured_commit or git_commit or "unknown"
    commit_source = "environment" if configured_commit else "git" if git_commit else "unknown"

    configured_dirty = _optional_bool(os.getenv("SIBYL_GIT_DIRTY"))
    git_status = None if configured_dirty is not None else _git_output("status", "--porcelain")
    dirty = (
        configured_dirty
        if configured_dirty is not None
        else bool(git_status)
        if git_status is not None
        else None
    )
    dirty_source = (
        "environment"
        if configured_dirty is not None
        else "git"
        if git_status is not None
        else "unknown"
    )

    return {
        "commit": commit,
        "commit_source": commit_source,
        "git_dirty": dirty,
        "git_status": "unknown" if dirty is None else "dirty" if dirty else "clean",
        "dirty_source": dirty_source,
    }
