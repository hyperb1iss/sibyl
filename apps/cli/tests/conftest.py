"""Keep CLI tests independent of the invoking shell's server credentials."""

import pytest


@pytest.fixture(autouse=True)
def isolated_cli_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("SIBYL_API_URL", "SIBYL_AUTH_TOKEN", "SIBYL_CONTEXT"):
        monkeypatch.delenv(variable, raising=False)
