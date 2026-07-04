from sibyl import _read_version


def test_read_version_prefers_deployment_env(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_VERSION", "1.1.0-rc.1")

    assert _read_version() == "1.1.0-rc.1"


def test_read_version_ignores_blank_deployment_env(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_VERSION", " ")

    assert _read_version() != ""
