import json

from sibyl.cli.auth_store import clear_access_token, read_auth_data, set_access_token, write_auth_data


def test_auth_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "auth.json"
    assert read_auth_data(path) == {}

    set_access_token("tok", path)
    assert read_auth_data(path)["access_token"] == "tok"

    clear_access_token(path)
    assert not path.exists()


def test_auth_store_preserves_other_fields(tmp_path) -> None:
    path = tmp_path / "auth.json"
    write_auth_data({"api_key": "k"}, path)
    set_access_token("tok", path)
    assert read_auth_data(path) == {"access_token": "tok", "api_key": "k"}

    clear_access_token(path)
    assert read_auth_data(path) == {"api_key": "k"}


def test_auth_store_ignores_invalid_json(tmp_path) -> None:
    path = tmp_path / "auth.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_auth_data(path) == {}

