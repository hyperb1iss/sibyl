"""Keep CLI tests independent of the invoking shell's server credentials."""

import pytest


@pytest.fixture(autouse=True)
def isolated_cli_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("SIBYL_API_URL", "SIBYL_AUTH_TOKEN", "SIBYL_CONTEXT"):
        monkeypatch.delenv(variable, raising=False)
    # The recall resolver falls back to the legacy defaults.project setting in
    # ~/.sibyl/config.toml; a developer's real config must not decide whether
    # a test sees a refusal. Tests that need a legacy default patch it back in.
    monkeypatch.setattr("sibyl_cli.project_scope.get_default_project", lambda: "")
