from __future__ import annotations

from pathlib import Path

from benchmarks import git_provenance as module


def test_git_provenance_marks_dirty_worktrees(monkeypatch) -> None:
    def fake_git_output(_root: Path, *args: str) -> str | None:
        if args == ("rev-parse", "HEAD"):
            return "abc123"
        if args == ("status", "--porcelain"):
            return " M benchmarks/longmemeval_bench.py"
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git_output", fake_git_output)

    assert module.git_provenance(Path(".")) == {
        "sibyl_commit": "abc123",
        "git_dirty": True,
        "git_status": "dirty",
    }
    assert module.git_provenance_metadata(Path(".")) == {
        "sibyl_commit": "abc123",
        "git_dirty": "true",
        "git_status": "dirty",
    }


def test_git_provenance_marks_unknown_when_git_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(module, "_git_output", lambda _root, *args: None)

    assert module.git_provenance(Path(".")) == {
        "sibyl_commit": "unknown",
        "git_dirty": None,
        "git_status": "unknown",
    }
