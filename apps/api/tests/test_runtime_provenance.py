from __future__ import annotations

import sibyl.runtime_provenance as provenance_module


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
