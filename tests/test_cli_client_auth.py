from sibyl.cli.client import _load_default_auth_token


def test_cli_auth_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_AUTH_TOKEN", " tok ")
    assert _load_default_auth_token() == "tok"
