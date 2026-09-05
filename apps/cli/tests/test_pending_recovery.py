"""Exercise queue ownership and recovery through the actual HTTP client."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from sibyl_cli import auth_store, client_transport, pending_writes
from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.pending_identity import normalize_replay_identity

BASE_URL = "https://testserver/api"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    client_transport._FAILURE_WINDOWS.clear()


@pytest.fixture
def identity() -> dict[str, Any]:
    return {
        "version": 1,
        "server_instance_id": "11111111-1111-1111-1111-111111111111",
        "user_id": "22222222-2222-2222-2222-222222222222",
        "organization_id": "33333333-3333-3333-3333-333333333333",
        "credential": {
            "kind": "session",
            "api_key_id": None,
            "scopes": [],
            "project_ids": None,
            "memory_space_ids": None,
            "memory_scope_keys": None,
        },
    }


def attach(client: SibylClient, handler: Any) -> SibylClient:
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers=client._default_headers(),
    )
    return client


def queued(
    identity: dict[str, Any] | None,
    *,
    scope: str | None = None,
    method: str = "POST",
    path: str = "/memory/raw",
) -> dict[str, Any]:
    return pending_writes.create_pending_write(
        method=method,
        path=path,
        base_url=BASE_URL,
        json_payload={"raw_content": "original payload"},
        params=None,
        replay_identity=identity,
        replay_scope=scope,
    )


@pytest.mark.asyncio
async def test_new_login_recovers_verified_owner_with_original_idempotency_key(
    identity: dict,
) -> None:
    auth_store.set_tokens(BASE_URL, "first-login", "first-refresh")
    first = SibylClient(base_url=BASE_URL)

    async def interrupted(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(200, json=identity)
        raise httpx.ReadTimeout("response lost", request=request)

    attach(first, interrupted)
    with pytest.raises(SibylClientError):
        await first.post("/memory/raw", json={"raw_content": "original payload"})
    await first.close()
    original = pending_writes.list_pending_writes()[0]
    assert original["replay_identity"] == identity
    first_scope = original["replay_scope"]
    auth_store.set_tokens(BASE_URL, "second-login", "second-refresh")
    calls = []

    def recovered(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(200, json=identity)
        return httpx.Response(200, json={"mutation_receipt": {"applied": True, "replayed": True}})

    second = attach(SibylClient(base_url=BASE_URL), recovered)
    assert second._replay_scope != first_scope
    await second._maybe_replay_pending_writes(ignore_backoff=True)
    await second.close()
    writes = [request for request in calls if request.method == "POST"]
    assert len(writes) == 1
    assert writes[0].headers["Idempotency-Key"] == original["idempotency_key"]
    assert writes[0].headers["Authorization"] == "Bearer second-login"
    assert pending_writes.list_pending_writes() == []
    assert pending_writes.read_pending_metrics()["replayed"] == 1


@pytest.mark.parametrize(
    "changed",
    [
        "server_instance_id",
        "user_id",
        "organization_id",
        "scopes",
        "project_ids",
        "memory_space_ids",
        "memory_scope_keys",
        "api_key_id",
    ],
)
@pytest.mark.asyncio
async def test_changed_owner_or_credential_restriction_never_sends(
    identity: dict, changed: str
) -> None:
    original = queued(identity)
    current = deepcopy(identity)
    if changed in ("server_instance_id", "user_id", "organization_id"):
        current[changed] = "44444444-4444-4444-4444-444444444444"
    elif changed == "api_key_id":
        current["credential"].update(
            kind="api_key", api_key_id="44444444-4444-4444-4444-444444444444"
        )
    else:
        current["credential"][changed] = ["limited"]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=current)

    client = attach(SibylClient(base_url=BASE_URL, auth_token="different-login"), handler)
    await client._maybe_replay_pending_writes(ignore_backoff=True)
    await client.close()
    assert len(calls) == 1
    assert pending_writes.read_pending_write(original["id"])["attempts"] == 0


@pytest.mark.asyncio
async def test_failed_identity_preflight_preserves_draft_without_sending_mutation() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            503, json={"error": "service_unavailable", "message": "private response"}
        )

    client = attach(SibylClient(base_url=BASE_URL, auth_token="synthetic"), handler)
    with pytest.raises(SibylClientError):
        await client.post("/memory/raw", json={"raw_content": "keep draft"})
    await client.close()
    assert [request.method for request in calls] == ["GET"]
    item = pending_writes.list_pending_writes()[0]
    assert item["json"] == {"raw_content": "keep draft"}
    assert item["last_failure"]["status_code"] == 503
    assert "private response" not in str(item)


@pytest.mark.asyncio
async def test_missing_task_parks_but_unrelated_capture_progresses(identity: dict) -> None:
    missing = queued(identity, method="PATCH", path="/tasks/123")
    later = queued(identity, path="/tasks/123/complete")
    capture = queued(identity)
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(200, json=identity)
        if request.url.path == "/api/tasks/123":
            return httpx.Response(404, json={"error": "not_found", "message": "Task missing"})
        assert request.url.path == "/api/memory/raw"
        assert request.headers["Idempotency-Key"] == capture["idempotency_key"]
        return httpx.Response(200, json={"mutation_receipt": {"applied": True, "replayed": True}})

    client = attach(SibylClient(base_url=BASE_URL, auth_token="synthetic"), handler)
    await client._maybe_replay_pending_writes(ignore_backoff=True)
    await client.close()
    assert "/api/tasks/123/complete" not in paths
    remaining = {item["id"]: item for item in pending_writes.list_pending_writes()}
    assert set(remaining) == {missing["id"], later["id"]}
    assert remaining[missing["id"]]["status"] == "attention"
    assert remaining[missing["id"]]["last_failure"]["status_code"] == 404
    assert remaining[later["id"]]["attempts"] == 0


@pytest.mark.asyncio
async def test_old_server_fallback_replays_only_original_lineage(identity: dict) -> None:
    client = SibylClient(base_url=BASE_URL, auth_token="synthetic")
    same = queued(None, scope=client._replay_scope)
    foreign = queued(None, scope="credential:another-login")
    owned = queued(identity, scope=client._replay_scope)
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(404, json={"detail": "Not found"})
        assert request.headers["Idempotency-Key"] == same["idempotency_key"]
        return httpx.Response(200, json={"ok": True})

    attach(client, handler)
    await client._maybe_replay_pending_writes(ignore_backoff=True)
    await client.close()
    assert len(paths) == 2
    assert {item["id"] for item in pending_writes.list_pending_writes()} == {
        foreign["id"],
        owned["id"],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_org",
        "invalid_uuid",
        "bool_version",
        "unknown_version",
        "missing_restrictions",
        "bad_scopes",
        "session_key",
        "unknown_kind",
    ],
)
def test_identity_normalization_rejects_incomplete_contract(identity: dict, mutation: str) -> None:
    candidate = deepcopy(identity)
    if mutation == "missing_org":
        candidate.pop("organization_id")
    elif mutation == "invalid_uuid":
        candidate["server_instance_id"] = "server"
    elif mutation == "bool_version":
        candidate["version"] = True
    elif mutation == "unknown_version":
        candidate["version"] = 2
    elif mutation == "missing_restrictions":
        candidate["credential"].pop("project_ids")
    elif mutation == "bad_scopes":
        candidate["credential"]["scopes"] = "read"
    elif mutation == "session_key":
        candidate["credential"]["api_key_id"] = identity["user_id"]
    else:
        candidate["credential"]["kind"] = "anonymous"
    assert normalize_replay_identity(candidate) is None


def test_identity_normalization_canonicalizes_set_restrictions(identity: dict) -> None:
    identity["credential"]["scopes"] = ["write", "read", "read"]
    normalized = normalize_replay_identity(identity)
    assert normalized is not None
    assert normalized["credential"]["scopes"] == ["read", "write"]
    assert identity["credential"]["scopes"] == ["write", "read", "read"]


@pytest.mark.parametrize("kind", [[], {}, 1, None])
def test_identity_normalization_rejects_malformed_credential_kind(
    identity: dict, kind: Any
) -> None:
    identity["credential"]["kind"] = kind
    assert normalize_replay_identity(identity) is None


@pytest.mark.asyncio
async def test_attention_entry_blocks_its_resource_on_subsequent_replay(identity: dict) -> None:
    first = queued(identity, method="PATCH", path="/tasks/123")
    later = queued(identity, path="/tasks/123/complete")
    capture = queued(identity)
    pending_writes.record_pending_failure(first["id"], category="rejected", status_code=404)
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(200, json=identity)
        sent.append(request.url.path)
        assert request.headers["Idempotency-Key"] == capture["idempotency_key"]
        return httpx.Response(200, json={"ok": True})

    client = attach(SibylClient(base_url=BASE_URL, auth_token="synthetic"), handler)
    await client._maybe_replay_pending_writes(ignore_backoff=True)
    await client.close()
    assert sent == ["/api/memory/raw"]
    assert pending_writes.read_pending_write(later["id"])["attempts"] == 0


@pytest.mark.asyncio
async def test_unresolved_bulk_is_barrier_for_later_writes(identity: dict) -> None:
    first = queued(identity, path="/entities/bulk")
    later = queued(identity)
    pending_writes.record_pending_failure(first["id"], category="conflict", status_code=409)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=identity)

    client = attach(SibylClient(base_url=BASE_URL, auth_token="synthetic"), handler)
    await client._maybe_replay_pending_writes(ignore_backoff=True)
    await client.close()
    assert pending_writes.read_pending_write(later["id"])["attempts"] == 0


@pytest.mark.asyncio
async def test_new_mutation_waits_for_older_related_attention(identity: dict) -> None:
    prior = queued(identity, method="PATCH", path="/tasks/123")
    pending_writes.record_pending_failure(prior["id"], category="rejected", status_code=404)
    mutations = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(200, json=identity)
        mutations.append(request)
        return httpx.Response(200, json={"ok": True})

    client = attach(SibylClient(base_url=BASE_URL, auth_token="synthetic"), handler)
    with pytest.raises(SibylClientError) as failure:
        await client.post("/tasks/123/complete", json={})
    await client.close()
    assert failure.value.error_code == "pending_dependency"
    assert not mutations
    remaining = pending_writes.list_pending_writes()
    assert len(remaining) == 2
    successor = next(item for item in remaining if item["id"] != prior["id"])
    assert successor["last_failure"]["category"] == "dependency"
    assert successor["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["protocol", "invalid_json"])
async def test_unconfirmed_response_retains_payload_and_diagnostic(
    identity: dict, failure_kind: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(200, json=identity)
        if failure_kind == "protocol":
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        return httpx.Response(200, content=b"invalid response")

    client = attach(SibylClient(base_url=BASE_URL, auth_token="synthetic"), handler)
    with pytest.raises(SibylClientError) as failure:
        await client.post("/memory/raw", json={"raw_content": "keep me"})
    await client.close()
    assert failure.value.error_code == "response_unconfirmed"
    entry = pending_writes.list_pending_writes()[0]
    assert entry["json"] == {"raw_content": "keep me"}
    assert entry["last_failure"]["category"] == (
        "transport" if failure_kind == "protocol" else "server"
    )
    assert entry["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["idempotency_in_progress", "entity_locked"])
async def test_transient_lock_retries_original_request(identity: dict, error_code: str) -> None:
    item = queued(identity)
    writes = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(200, json=identity)
        writes.append(request)
        if len(writes) == 1:
            return httpx.Response(409, json={"detail": {
                "error": error_code, "message": "Still running"
            }})
        return httpx.Response(200, json={"ok": True})

    client = attach(SibylClient(base_url=BASE_URL, auth_token="synthetic"), handler)
    await client._maybe_replay_pending_writes(ignore_backoff=True)
    failed = pending_writes.read_pending_write(item["id"])
    assert failed["status"] == "pending"
    assert failed["last_failure"]["error_code"] == error_code
    await client._maybe_replay_pending_writes(ignore_backoff=True)
    await client.close()
    assert len(writes) == 2
    assert {request.headers["Idempotency-Key"] for request in writes} == {item["idempotency_key"]}
    assert pending_writes.list_pending_writes() == []


@pytest.mark.asyncio
async def test_new_write_uses_fresh_identity_without_reassigning_older_write(identity: dict) -> None:
    auth_store.set_tokens(BASE_URL, "synthetic", "refresh")
    auth_store.cache_pending_replay_identity(BASE_URL, "synthetic", identity)
    older = queued(identity)
    current = deepcopy(identity)
    current["server_instance_id"] = "44444444-4444-4444-4444-444444444444"
    writes = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(200, json=current)
        writes.append(request)
        assert request.headers["X-Sibyl-Server-Instance"] == current["server_instance_id"]
        return httpx.Response(200, json={"ok": True})

    client = attach(SibylClient(base_url=BASE_URL), handler)
    assert client._pending_identity == identity
    await client.post("/memory/raw", json={"raw_content": "new draft"})
    await client.close()
    assert len(writes) == 1
    assert pending_writes.read_pending_write(older["id"])["replay_identity"] == identity
    assert len(pending_writes.list_pending_writes()) == 1


@pytest.mark.asyncio
async def test_offline_draft_keeps_last_verified_owner(identity: dict) -> None:
    auth_store.set_tokens(BASE_URL, "synthetic", "refresh")
    auth_store.cache_pending_replay_identity(BASE_URL, "synthetic", identity)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = attach(SibylClient(base_url=BASE_URL), handler)
    with pytest.raises(SibylClientError):
        await client.post("/memory/raw", json={"raw_content": "offline draft"})
    await client.close()
    item = pending_writes.list_pending_writes()[0]
    assert item["replay_identity"] == identity
    assert item["json"] == {"raw_content": "offline draft"}
    assert item["last_failure"]["category"] == "transport"
