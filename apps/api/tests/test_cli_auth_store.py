import os

from sibyl.cli.auth_store import (
    clear_tokens,
    get_access_token,
    read_auth_data,
    set_tokens,
    write_auth_data,
)

TEST_API_URL = "http://localhost:3334/api"


def test_auth_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "auth.json"
    assert read_auth_data(path) == {}

    set_tokens(TEST_API_URL, "tok", path=path)
    assert get_access_token(TEST_API_URL, path) == "tok"

    clear_tokens(TEST_API_URL, path)
    # After clearing, the token should be gone but servers dict might still exist
    assert get_access_token(TEST_API_URL, path) is None


def test_auth_store_preserves_other_fields(tmp_path) -> None:
    path = tmp_path / "auth.json"
    write_auth_data({"other_key": "value"}, path)
    set_tokens(TEST_API_URL, "tok", path=path)

    data = read_auth_data(path)
    assert data.get("other_key") == "value"
    assert get_access_token(TEST_API_URL, path) == "tok"

    clear_tokens(TEST_API_URL, path)
    data = read_auth_data(path)
    assert data.get("other_key") == "value"
    assert get_access_token(TEST_API_URL, path) is None


def test_auth_store_ignores_invalid_json(tmp_path) -> None:
    path = tmp_path / "auth.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_auth_data(path) == {}


def test_auth_store_multiple_servers(tmp_path) -> None:
    path = tmp_path / "auth.json"
    url1 = "http://localhost:3334/api"
    url2 = "https://sibyl.example.com/api"

    set_tokens(url1, "token1", path=path)
    set_tokens(url2, "token2", path=path)

    assert get_access_token(url1, path) == "token1"
    assert get_access_token(url2, path) == "token2"

    # Clearing one doesn't affect the other
    clear_tokens(url1, path)
    assert get_access_token(url1, path) is None
    assert get_access_token(url2, path) == "token2"


def test_auth_store_with_refresh_token(tmp_path) -> None:
    from sibyl.cli.auth_store import get_refresh_token

    path = tmp_path / "auth.json"
    set_tokens(TEST_API_URL, "access", refresh_token="refresh", expires_in=3600, path=path)  # noqa: S106

    assert get_access_token(TEST_API_URL, path) == "access"
    assert get_refresh_token(TEST_API_URL, path) == "refresh"


def test_auth_store_sets_secure_file_permissions(tmp_path) -> None:
    if os.name == "nt":
        return
    path = tmp_path / "auth.json"
    set_tokens(TEST_API_URL, "tok", path=path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
