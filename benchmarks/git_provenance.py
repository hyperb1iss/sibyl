"""Git provenance helpers for benchmark artifacts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def _git_output(root: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - git path is resolved and args are fixed.
            [git, *args],
            check=True,
            capture_output=True,
            cwd=root,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def git_provenance(root: Path) -> dict[str, Any]:
    status = _git_output(root, "status", "--porcelain")
    git_dirty: bool | None = None
    git_status = "unknown"
    if status is not None:
        git_dirty = bool(status)
        git_status = "dirty" if git_dirty else "clean"
    return {
        "sibyl_commit": _git_output(root, "rev-parse", "HEAD") or "unknown",
        "git_dirty": git_dirty,
        "git_status": git_status,
    }


def git_provenance_metadata(root: Path) -> dict[str, str]:
    provenance = git_provenance(root)
    git_dirty = provenance["git_dirty"]
    return {
        "sibyl_commit": str(provenance["sibyl_commit"]),
        "git_dirty": "unknown" if git_dirty is None else str(git_dirty).lower(),
        "git_status": str(provenance["git_status"]),
    }
