from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks import longmemeval_v2_official_source as source


def _write_official_checkout(root: Path) -> tuple[Path, str]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required for the official-source test fixture")
    memory_modules = root / "memory_modules"
    evaluation = root / "evaluation"
    memory_modules.mkdir(parents=True)
    evaluation.mkdir()
    (memory_modules / "__init__.py").write_text("", encoding="utf-8")
    (memory_modules / "memory.py").write_text(
        """from __future__ import annotations

import threading

MemoryContextItem = dict[str, str]


class Memory:
    def __init__(self, memory_params: dict[str, object]) -> None:
        self.memory_params = dict(memory_params)
        self._query_context_local = threading.local()

    def set_query_context(self, *, query_invocation_id: str) -> None:
        if not query_invocation_id.strip():
            raise RuntimeError("query_invocation_id must be a non-empty string")
        self._query_context_local.context = {
            "query_invocation_id": query_invocation_id.strip(),
        }

    def clear_query_context(self) -> None:
        self._query_context_local.context = {}

    def get_query_context(self) -> dict[str, str]:
        return dict(getattr(self._query_context_local, "context", {}))


def register_memory(memory_cls: type[Memory]) -> type[Memory]:
    return memory_cls
""",
        encoding="utf-8",
    )
    (evaluation / "harness.py").write_text("def main():\n    return None\n", encoding="utf-8")
    subprocess.run([git, "init", "-q"], cwd=root, check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [git, "config", "user.email", "test@example.test"], cwd=root, check=True
    )
    subprocess.run(  # noqa: S603
        [git, "config", "user.name", "Test"], cwd=root, check=True
    )
    subprocess.run([git, "add", "."], cwd=root, check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [git, "commit", "-q", "-m", "fixture"], cwd=root, check=True
    )
    commit = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, commit


def test_source_record_carries_pin_diff_and_cleanliness(tmp_path: Path) -> None:
    repo, commit = _write_official_checkout(tmp_path / "official")

    record = source.official_source_record(repo, expected_commit=commit)

    assert record == {
        "url": source.OFFICIAL_REPO_URL,
        "path": str(repo),
        "commit": commit,
        "expected_commit": commit,
        "pin_matches": True,
        "git_status": "clean",
        "harness_path": source.OFFICIAL_HARNESS_PATH,
        "harness_exists": True,
        "previous_reviewed_commit": source.OFFICIAL_HARNESS_PREVIOUS_COMMIT,
        "reviewed_diff_url": source.OFFICIAL_HARNESS_DIFF_URL,
    }


def test_pinned_source_rejects_commit_drift_and_dirty_checkout(tmp_path: Path) -> None:
    repo, commit = _write_official_checkout(tmp_path / "official")

    with pytest.raises(RuntimeError, match="does not match reviewed pin"):
        source.require_pinned_source(repo, expected_commit="0" * 40)

    (repo / "evaluation" / "harness.py").write_text("changed = True\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="official checkout is dirty"):
        source.require_pinned_source(repo, expected_commit=commit)


def test_adapter_contract_runs_against_clean_official_base(tmp_path: Path) -> None:
    repo, commit = _write_official_checkout(tmp_path / "official")
    code = """
import json
import sys
from pathlib import Path
from benchmarks.longmemeval_v2_official_source import validate_contract

print(json.dumps(validate_contract(Path(sys.argv[1]), expected_commit=sys.argv[2])))
"""

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(repo), commit],
        check=True,
        capture_output=True,
        text=True,
    )
    receipt = json.loads(completed.stdout)

    assert receipt["status"] == "PASS"
    assert receipt["adapter_contract"] == {
        "base_signature": "(self, *, query_invocation_id: 'str') -> 'None'",
        "stored_context_keys": ["query_invocation_id"],
        "raw_question_metadata_rejected": True,
    }
