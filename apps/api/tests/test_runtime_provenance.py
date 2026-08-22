from __future__ import annotations

from types import SimpleNamespace

import sibyl.runtime_provenance as provenance_module


def test_git_output_preserves_successful_empty_stdout(monkeypatch) -> None:
    monkeypatch.setattr(provenance_module.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(
        provenance_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="\n"),
    )

    assert provenance_module._git_output("status", "--porcelain") == ""


def test_runtime_provenance_prefers_explicit_build_environment(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_GIT_COMMIT", "abc123")
    monkeypatch.setenv("SIBYL_GIT_DIRTY", "false")
    provenance_module.get_runtime_provenance.cache_clear()

    provenance = provenance_module.get_runtime_provenance()

    assert provenance == {
        "commit": "abc123",
        "commit_source": "environment",
        "git_dirty": False,
        "git_status": "clean",
        "dirty_source": "environment",
    }
    provenance_module.get_runtime_provenance.cache_clear()


def test_runtime_provenance_reports_unknown_without_env_or_git(monkeypatch) -> None:
    monkeypatch.delenv("SIBYL_GIT_COMMIT", raising=False)
    monkeypatch.delenv("SIBYL_GIT_DIRTY", raising=False)
    monkeypatch.setattr(provenance_module, "_git_output", lambda *_args: None)
    provenance_module.get_runtime_provenance.cache_clear()

    provenance = provenance_module.get_runtime_provenance()

    assert provenance == {
        "commit": "unknown",
        "commit_source": "unknown",
        "git_dirty": None,
        "git_status": "unknown",
        "dirty_source": "unknown",
    }
    provenance_module.get_runtime_provenance.cache_clear()


def test_runtime_provenance_reports_clean_git_checkout(monkeypatch) -> None:
    monkeypatch.delenv("SIBYL_GIT_COMMIT", raising=False)
    monkeypatch.delenv("SIBYL_GIT_DIRTY", raising=False)
    monkeypatch.setattr(
        provenance_module,
        "_git_output",
        lambda *args: "abc123" if args[0] == "rev-parse" else "",
    )
    provenance_module.get_runtime_provenance.cache_clear()

    provenance = provenance_module.get_runtime_provenance()

    assert provenance == {
        "commit": "abc123",
        "commit_source": "git",
        "git_dirty": False,
        "git_status": "clean",
        "dirty_source": "git",
    }
    provenance_module.get_runtime_provenance.cache_clear()
