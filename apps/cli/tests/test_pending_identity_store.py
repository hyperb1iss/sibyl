from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from sibyl_cli import auth_store


def test_verified_owner_cache_survives_refresh_but_not_new_login(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    url = "http://server.test/api"
    owner = {
        "version": 1,
        "server_instance_id": str(uuid4()),
        "user_id": str(uuid4()),
        "organization_id": str(uuid4()),
        "credential": {
            "kind": "session", "api_key_id": None, "scopes": [],
            "project_ids": None, "memory_space_ids": None, "memory_scope_keys": None,
        },
    }
    auth_store.set_tokens(url, "first", path=path)
    scope = auth_store.get_pending_replay_scope(url, path)
    assert auth_store.cache_pending_replay_identity(url, "first", owner, path=path)
    auth_store.set_tokens(url, "refreshed", path=path, pending_replay_scope=scope)
    assert auth_store.read_server_credentials(url, path)["pending_replay_identity"] == owner
    auth_store.set_tokens(url, "new-login", path=path)
    assert "pending_replay_identity" not in auth_store.read_server_credentials(url, path)
    assert not auth_store.cache_pending_replay_identity(url, "refreshed", owner, path=path)
    assert "pending_replay_identity" not in auth_store.read_server_credentials(url, path)


def test_cached_ownership_cannot_leak_between_contexts(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    url = "http://server.test/api"
    auth_store.set_tokens(url, "a", path=path, credential_scope="org-a")
    auth_store.set_tokens(url, "b", path=path, credential_scope="org-b")
    before = deepcopy(auth_store.read_auth_data(path))
    assert not auth_store.cache_pending_replay_identity(
        url, "a", {"user_id": "not-the-current-token"}, path=path, credential_scope="org-b"
    )
    assert auth_store.read_auth_data(path) == before
