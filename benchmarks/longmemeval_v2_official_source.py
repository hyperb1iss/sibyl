#!/usr/bin/env python3
"""Verify and describe Sibyl's pinned LongMemEval-V2 source contract."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OFFICIAL_REPO_URL = "https://github.com/xiaowu0162/LongMemEval-V2"
OFFICIAL_HARNESS_PATH = "evaluation/harness.py"
OFFICIAL_HARNESS_COMMIT = "2cc8c540bdb87fe6761629b585e727e1c4704520"
OFFICIAL_HARNESS_PREVIOUS_COMMIT = "be15ea6e995462f3391c1a610892df3f67dfa7bd"
OFFICIAL_HARNESS_DIFF_URL = (
    f"{OFFICIAL_REPO_URL}/compare/{OFFICIAL_HARNESS_PREVIOUS_COMMIT}...{OFFICIAL_HARNESS_COMMIT}"
)


def _git(repo: Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required to verify the official LongMemEval-V2 source")
    completed = subprocess.run(  # noqa: S603
        [executable, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def official_source_record(
    repo: Path | None,
    *,
    expected_commit: str = OFFICIAL_HARNESS_COMMIT,
) -> dict[str, object]:
    """Return the immutable source identity carried by plans and receipts."""
    commit = _git(repo, "rev-parse", "HEAD") if repo is not None else None
    git_status = (
        _git(
            repo,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
        if repo is not None
        else None
    )
    harness_exists = bool(repo is not None and (repo / OFFICIAL_HARNESS_PATH).is_file())
    return {
        "url": OFFICIAL_REPO_URL,
        "path": str(repo) if repo is not None else None,
        "commit": commit,
        "expected_commit": expected_commit,
        "pin_matches": commit == expected_commit,
        "git_status": (
            "not_configured" if repo is None else "clean" if git_status == "" else "dirty"
        ),
        "harness_path": OFFICIAL_HARNESS_PATH,
        "harness_exists": harness_exists,
        "previous_reviewed_commit": OFFICIAL_HARNESS_PREVIOUS_COMMIT,
        "reviewed_diff_url": OFFICIAL_HARNESS_DIFF_URL,
    }


def require_pinned_source(
    repo: Path,
    *,
    expected_commit: str = OFFICIAL_HARNESS_COMMIT,
) -> dict[str, object]:
    """Fail unless the checkout is the reviewed, immutable, clean source."""
    record = official_source_record(repo, expected_commit=expected_commit)
    failures = []
    if record["harness_exists"] is not True:
        failures.append(f"missing {OFFICIAL_HARNESS_PATH}")
    if record["pin_matches"] is not True:
        failures.append(
            f"commit {record['commit']!r} does not match reviewed pin {expected_commit}"
        )
    if record["git_status"] != "clean":
        failures.append("official checkout is dirty")
    if failures:
        raise RuntimeError("; ".join(failures))
    return record


def require_identifier_only_adapter(repo: Path) -> dict[str, object]:
    """Load the adapter against the official base and prove context isolation."""
    sys.path.insert(0, str(repo))
    try:
        official_memory = importlib.import_module("memory_modules.memory")
        adapter = importlib.import_module("benchmarks.longmemeval_v2_memory.sibyl_memory")
        if not issubclass(adapter.SibylLiveApiMemory, official_memory.Memory):
            raise TypeError("Sibyl adapter is not bound to the pinned official Memory base")
        signature = inspect.signature(official_memory.Memory.set_query_context)
        parameters = list(signature.parameters.values())
        expected = [
            ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            ("query_invocation_id", inspect.Parameter.KEYWORD_ONLY),
        ]
        actual = [(parameter.name, parameter.kind) for parameter in parameters]
        if actual != expected:
            raise RuntimeError(f"official query-context signature changed: {signature}")

        memory = adapter.SibylLiveApiMemory.__new__(adapter.SibylLiveApiMemory)
        official_memory.Memory.__init__(memory, {})
        memory.set_query_context(query_invocation_id="opaque-invocation")
        context = memory.get_query_context()
        if context != {"query_invocation_id": "opaque-invocation"}:
            raise RuntimeError(f"adapter leaked query metadata: {context!r}")
        try:
            memory.set_query_context(question_item={"question": "must not cross"})
        except TypeError:
            pass
        else:
            raise RuntimeError("adapter accepted raw question metadata")
        return {
            "base_signature": str(signature),
            "stored_context_keys": sorted(context),
            "raw_question_metadata_rejected": True,
        }
    finally:
        sys.path.remove(str(repo))


def validate_contract(
    repo: Path,
    *,
    expected_commit: str = OFFICIAL_HARNESS_COMMIT,
) -> dict[str, Any]:
    source = require_pinned_source(repo, expected_commit=expected_commit)
    adapter = require_identifier_only_adapter(repo)
    return {
        "status": "PASS",
        "official_source": source,
        "adapter_contract": adapter,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-repo", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    receipt = validate_contract(Path(args.official_repo).expanduser().resolve())
    print(json.dumps(receipt, indent=2, sort_keys=True))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
