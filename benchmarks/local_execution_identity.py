"""Canonical local checkout identity for paid benchmark execution."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ASCII_DELETE_CODEPOINT = 127
GIT_REF_FORBIDDEN_ASCII_MAX = 32
GIT_SHA_LENGTH = 40
LS_REMOTE_FIELD_COUNT = 2
GITHUB_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}"
)


def _required_git_output(root: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise ValueError("local experiment identity requires git")
    try:
        result = subprocess.run(  # noqa: S603
            [git, *args],
            check=True,
            capture_output=True,
            cwd=root,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("local experiment identity could not inspect the Sibyl checkout") from exc
    return result.stdout.strip()


def is_canonical_repository(repository: str) -> bool:
    """Return whether a repository is one canonical GitHub owner/name slug."""
    return bool(GITHUB_REPOSITORY_PATTERN.fullmatch(repository))


def is_valid_branch_ref(ref: str) -> bool:
    """Apply Git-compatible strict validation to one full branch ref."""
    prefix = "refs/heads/"
    if not ref.startswith(prefix) or ref != ref.strip():
        return False
    branch = ref.removeprefix(prefix)
    if not branch or branch.startswith("-") or branch.endswith("."):
        return False
    if ".." in branch or "@{" in branch or "\\" in branch:
        return False
    if any(
        ord(character) <= GIT_REF_FORBIDDEN_ASCII_MAX or ord(character) == ASCII_DELETE_CODEPOINT
        for character in branch
    ):
        return False
    if any(character in "~^:?*[" for character in branch):
        return False
    components = branch.split("/")
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in components
    )


def _repository_from_remote(remote: str) -> str:
    value = remote.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com":
            raise ValueError("local experiment origin must be a GitHub repository")
        value = parsed.path.lstrip("/")
    if not is_canonical_repository(value):
        raise ValueError("local experiment origin has no canonical owner/repository identity")
    return value


def _require_published_ref(root: Path, *, ref: str, sha: str) -> None:
    try:
        published_ref = _required_git_output(
            root,
            "ls-remote",
            "--exit-code",
            "--refs",
            "origin",
            ref,
        )
    except ValueError as exc:
        raise ValueError("local execution could not verify its exact ref on origin") from exc
    lines = published_ref.splitlines()
    if len(lines) != 1:
        raise ValueError("local execution origin returned no unique exact ref")
    fields = lines[0].split("\t")
    if len(fields) != LS_REMOTE_FIELD_COUNT:
        raise ValueError("local execution origin returned a malformed exact ref")
    published_sha, published_name = fields
    if (
        len(published_sha) != GIT_SHA_LENGTH
        or any(character not in "0123456789abcdef" for character in published_sha)
        or published_name != ref
    ):
        raise ValueError("local execution origin returned a malformed exact ref")
    if published_sha != sha:
        raise ValueError("local execution SHA differs from the exact ref on origin")


def require_local_checkout(root: Path) -> dict[str, Any]:
    """Return a clean checkout identity proven against its live origin ref."""
    repository = _repository_from_remote(_required_git_output(root, "remote", "get-url", "origin"))
    ref = _required_git_output(root, "symbolic-ref", "-q", "HEAD")
    sha = _required_git_output(root, "rev-parse", "HEAD")
    if not is_valid_branch_ref(ref):
        raise ValueError("local execution ref must be a valid full refs/heads/* branch ref")
    if len(sha) != GIT_SHA_LENGTH or any(character not in "0123456789abcdef" for character in sha):
        raise ValueError("local execution SHA must be a full lowercase Git SHA")
    remote_ref = ref.replace("refs/heads/", "refs/remotes/origin/", 1)
    try:
        remote_sha = _required_git_output(root, "rev-parse", "--verify", remote_ref)
    except ValueError as exc:
        raise ValueError("local execution ref has no exact origin tracking ref") from exc
    status = _required_git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status:
        raise ValueError("local experiment runs require a clean Sibyl checkout")
    if remote_sha != sha:
        raise ValueError("local execution SHA differs from its origin tracking ref")
    _require_published_ref(root, ref=ref, sha=sha)
    source = {"repository": repository, "ref": ref, "sha": sha}
    return {
        "source_identity": source,
        "provenance": {
            "sibyl_commit": sha,
            "git_dirty": False,
            "git_status": "clean",
        },
    }
