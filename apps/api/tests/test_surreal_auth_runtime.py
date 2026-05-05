from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sibyl.auth.api_key_common import api_key_prefix, hash_api_key
from sibyl.auth.passwords import hash_password
from sibyl.auth.primitives import DeviceTokenError
from sibyl.db.models import ProjectRole
from sibyl.persistence.surreal import auth as surreal_auth, auth_runtime as surreal_auth_runtime
from sibyl_core.auth import AuthSession


class _StaticAuthClientScope:
    def __init__(self, client: object) -> None:
        self._client = client

    async def __aenter__(self) -> object:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _RecordingAuthClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        return self.response


class _SequenceAuthClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        if not self.responses:
            raise AssertionError("unexpected query")
        return self.responses.pop(0)

    async def execute_query_raw(self, query: str, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        if not self.responses:
            raise AssertionError("unexpected query")
        return self.responses.pop(0)


def _auth_session(*, organization_id=None) -> AuthSession:
    return AuthSession(
        id=uuid4(),
        user_id=uuid4(),
        organization_id=organization_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
        last_active_at=datetime.now(UTC),
        device_name="mcp_oauth",
        device_type="mcp",
    )


@pytest.mark.asyncio
async def test_surreal_auth_client_scope_reuses_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[SimpleNamespace] = []

    async def close_client(client: SimpleNamespace) -> None:
        client.closed = True

    def build_client() -> SimpleNamespace:
        client = SimpleNamespace(closed=False)
        client.close = lambda: close_client(client)
        clients.append(client)
        return client

    await surreal_auth.close_shared_surreal_auth_client()
    monkeypatch.setattr(surreal_auth, "build_surreal_auth_client", build_client)

    try:
        async with (
            surreal_auth.surreal_auth_client_scope() as first,
            surreal_auth.surreal_auth_client_scope() as second,
        ):
            assert first is second
        assert clients == [first]
        assert clients[0].closed is False
    finally:
        await surreal_auth.close_shared_surreal_auth_client()

    assert clients[0].closed is True


def test_surreal_auth_runtime_exports_neutral_surface() -> None:
    assert "start_device_authorization" in surreal_auth_runtime.__all__
    assert "exchange_device_code" in surreal_auth_runtime.__all__
    assert "list_accessible_project_graph_ids" in surreal_auth_runtime.__all__
    assert "resolve_auth_context" in surreal_auth_runtime.__all__
    assert "validate_access_session" in surreal_auth_runtime.__all__


def test_coerce_datetime_normalizes_aware_datetime_instances() -> None:
    aware = datetime(2026, 4, 23, 16, 0, tzinfo=UTC)

    actual = surreal_auth_runtime._coerce_datetime(aware)

    assert actual == aware.replace(tzinfo=None)
    assert actual is not None
    assert actual.tzinfo is None


def test_surreal_session_repository_accepts_aware_expiry_datetimes() -> None:
    repo = surreal_auth_runtime.SurrealSessionRepository(object())
    record = {
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "refresh_token_expires_at": datetime.now(UTC) + timedelta(days=1),
        "revoked_at": None,
    }

    assert repo._is_session_active(record) is True
    assert repo._has_refresh_session(record) is True


@pytest.mark.asyncio
async def test_session_record_helpers_return_repository_session_without_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _auth_session(organization_id=uuid4())
    sessions = SimpleNamespace(
        create_session=AsyncMock(return_value=session),
        get_session_by_refresh_token=AsyncMock(return_value=session),
        rotate_tokens=AsyncMock(return_value=session),
    )
    select_one = AsyncMock(side_effect=AssertionError("unexpected session reload"))

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(object()),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: sessions,
    )
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "select_one", select_one)

    created = await surreal_auth_runtime.create_session_record(
        user_id=session.user_id,
        organization_id=session.organization_id,
        token="access-token",
        expires_at=session.expires_at,
    )
    loaded = await surreal_auth_runtime.load_refresh_session_record("refresh-token")
    rotated = await surreal_auth_runtime.rotate_refresh_session_record(
        "refresh-token",
        new_access_token="new-access",
        new_access_expires_at=session.expires_at,
        new_refresh_token="new-refresh",
        new_refresh_expires_at=session.refresh_token_expires_at,
    )

    assert created.id == session.id
    assert loaded.id == session.id
    assert rotated.id == session.id
    assert created.user_id == session.user_id
    assert created.organization_id == session.organization_id
    select_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_revoke_helpers_revoke_loaded_session_without_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _auth_session()
    sessions = SimpleNamespace(
        get_session_by_token=AsyncMock(return_value=session),
        get_session_by_refresh_token=AsyncMock(return_value=session),
        revoke_loaded_session=AsyncMock(return_value=True),
    )
    select_one = AsyncMock(side_effect=AssertionError("unexpected session reload"))

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(object()),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: sessions,
    )
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "select_one", select_one)

    await surreal_auth_runtime.revoke_access_session("access-token")
    await surreal_auth_runtime.revoke_refresh_session_record("refresh-token")

    assert sessions.revoke_loaded_session.await_count == 2
    sessions.revoke_loaded_session.assert_any_await(session)
    select_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_access_session_uses_sid_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _auth_session()
    sessions = SimpleNamespace(
        get_session_by_id=AsyncMock(return_value=session),
        get_session_by_token=AsyncMock(return_value=None),
        revoke_loaded_session=AsyncMock(return_value=True),
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(object()),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: sessions,
    )
    monkeypatch.setattr(
        surreal_auth_runtime,
        "decode_token_unverified",
        lambda token: {"sid": str(session.id)},
    )

    await surreal_auth_runtime.revoke_access_session("access-token")

    sessions.get_session_by_id.assert_awaited_once_with(session.id)
    sessions.get_session_by_token.assert_not_awaited()
    sessions.revoke_loaded_session.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_validate_access_session_checks_active_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _auth_session()
    sessions = SimpleNamespace(get_session_by_token=AsyncMock(return_value=session))

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(object()),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: sessions,
    )

    assert await surreal_auth_runtime.validate_access_session("access-token") is True
    sessions.get_session_by_token.assert_awaited_once_with("access-token")


@pytest.mark.asyncio
async def test_validate_access_session_uses_sid_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _auth_session()
    sessions = SimpleNamespace(
        get_session_by_id=AsyncMock(return_value=session),
        get_session_by_token=AsyncMock(return_value=None),
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(object()),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: sessions,
    )
    monkeypatch.setattr(
        surreal_auth_runtime,
        "decode_token_unverified",
        lambda token: {"sid": str(session.id)},
    )

    assert await surreal_auth_runtime.validate_access_session("access-token") is True
    sessions.get_session_by_id.assert_awaited_once_with(session.id)
    sessions.get_session_by_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_access_session_rejects_revoked_or_missing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = SimpleNamespace(get_session_by_token=AsyncMock(return_value=None))

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(object()),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: sessions,
    )

    assert await surreal_auth_runtime.validate_access_session("access-token") is False
    sessions.get_session_by_token.assert_awaited_once_with("access-token")


@pytest.mark.asyncio
async def test_get_session_by_id_uses_uuid_lookup() -> None:
    session_id = uuid4()
    user_id = uuid4()
    session_record = {
        "uuid": str(session_id),
        "user_id": str(user_id),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "refresh_token_expires_at": datetime.now(UTC) + timedelta(days=30),
        "revoked_at": None,
    }
    client = _RecordingAuthClient([session_record])
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    session = await repo.get_session_by_id(session_id)

    assert session is not None
    assert session.id == session_id
    assert session.user_id == user_id
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "WHERE uuid = $uuid" in query
    assert "token_hash" not in query
    assert params == {"uuid": str(session_id)}


@pytest.mark.asyncio
async def test_rotate_tokens_updates_loaded_session_without_reload() -> None:
    session = _auth_session(organization_id=uuid4())
    new_access_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    new_refresh_expires_at = datetime.now(UTC) + timedelta(days=30)
    updated_record = {
        "uuid": str(session.id),
        "user_id": str(session.user_id),
        "organization_id": str(session.organization_id),
        "expires_at": new_access_expires_at,
        "refresh_token_expires_at": new_refresh_expires_at,
        "revoked_at": None,
        "version": 1,
    }
    client = _RecordingAuthClient([updated_record])
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    rotated = await repo.rotate_tokens(
        session,
        new_access_token="new-access",
        new_access_expires_at=new_access_expires_at,
        new_refresh_token="new-refresh",
        new_refresh_expires_at=new_refresh_expires_at,
    )

    assert rotated.id == session.id
    assert rotated.user_id == session.user_id
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert query.lstrip().startswith("UPDATE user_sessions")
    assert "SELECT * FROM user_sessions" not in query
    assert "UPSERT user_sessions" not in query
    assert "version = $next_version" in query
    assert "version = $expected_version" in query
    assert params["uuid"] == str(session.id)
    assert params["expected_version"] == 0
    assert params["next_version"] == 1
    assert params["token_hash"] == repo.hash_token("new-access")
    assert params["refresh_token_hash"] == repo.hash_token("new-refresh")
    assert params["last_active_at"] == params["updated_at"]
    assert rotated.version == 1


@pytest.mark.asyncio
async def test_rotate_tokens_raises_lookup_error_on_version_conflict() -> None:
    session = _auth_session()
    client = _RecordingAuthClient([])
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    with pytest.raises(LookupError):
        await repo.rotate_tokens(
            session,
            new_access_token="new-access",
            new_access_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            new_refresh_token="new-refresh",
            new_refresh_expires_at=datetime.now(UTC) + timedelta(days=30),
        )

    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_rotate_refresh_exchange_returns_none_on_rotation_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _auth_session(organization_id=uuid4())
    sessions = SimpleNamespace(
        get_session_by_refresh_token=AsyncMock(side_effect=[session, None]),
        rotate_tokens=AsyncMock(side_effect=LookupError("conflict")),
    )
    audit = AsyncMock()

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(object()),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: sessions,
    )
    monkeypatch.setattr(
        surreal_auth_runtime,
        "create_access_token",
        lambda **kwargs: "access-token",
    )
    monkeypatch.setattr(
        surreal_auth_runtime,
        "create_refresh_token",
        lambda **kwargs: ("refresh-token", datetime.now(UTC) + timedelta(days=30)),
    )
    monkeypatch.setattr(surreal_auth_runtime, "_log_audit_event", audit)

    rotation = await surreal_auth_runtime.rotate_refresh_exchange(
        refresh_token="old-refresh",
        user_id=session.user_id,
        organization_id=session.organization_id,
        request=None,
    )

    assert rotation is None
    assert sessions.get_session_by_refresh_token.await_count == 2
    sessions.rotate_tokens.assert_awaited_once()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_surreal_repository_replace_record_uses_single_upsert_statement() -> None:
    session_id = uuid4()
    record = {"uuid": str(session_id), "token_hash": "token"}
    client = _RecordingAuthClient([record])
    repo = surreal_auth_runtime._SurrealRepository(client)

    saved = await repo.replace_record("user_sessions", uuid=session_id, record=record)

    assert saved["uuid"] == str(session_id)
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "UPSERT user_sessions CONTENT $record WHERE uuid = $uuid" in query
    assert "DELETE FROM user_sessions" not in query
    assert params == {"uuid": str(session_id), "record": record}


@pytest.mark.asyncio
async def test_authenticate_api_key_batches_last_used_and_project_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "sk_live_test-key"
    salt_hex, hash_hex = hash_api_key(raw_key)
    api_key_id = uuid4()
    user_id = uuid4()
    organization_id = uuid4()
    project_id = uuid4()
    client = _SequenceAuthClient(
        [
            [
                {
                    "uuid": str(api_key_id),
                    "user_id": str(user_id),
                    "organization_id": str(organization_id),
                    "key_prefix": api_key_prefix(raw_key),
                    "key_salt": salt_hex,
                    "key_hash": hash_hex,
                    "scopes": ["api:read"],
                    "revoked_at": None,
                    "expires_at": None,
                }
            ],
            {
                "result": [
                    {"status": "OK", "result": []},
                    {
                        "status": "OK",
                        "result": [
                            {
                                "uuid": str(uuid4()),
                                "api_key_id": str(api_key_id),
                                "project_id": str(project_id),
                            }
                        ],
                    },
                ]
            },
        ]
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )

    auth = await surreal_auth_runtime.authenticate_api_key(raw_key)

    assert auth is not None
    assert auth.api_key_id == api_key_id
    assert auth.user_id == user_id
    assert auth.organization_id == organization_id
    assert auth.scopes == ["api:read"]
    assert auth.project_ids == [project_id]
    assert len(client.calls) == 2
    scope_query, scope_params = client.calls[1]
    assert "UPDATE api_keys" in scope_query
    assert "revoked_at = NONE" in scope_query
    assert "SELECT * FROM api_key_project_scopes" in scope_query
    assert "UPSERT api_keys" not in scope_query
    assert scope_params["api_key_id"] == str(api_key_id)
    assert scope_params["last_used_at"] == scope_params["updated_at"]


@pytest.mark.asyncio
async def test_mark_current_updates_session_flags_without_loading_all_sessions() -> None:
    user_id = uuid4()
    session_id = uuid4()
    token = "access-token"
    repo = surreal_auth_runtime.SurrealSessionRepository(object())
    session_record = {
        "uuid": str(session_id),
        "user_id": str(user_id),
        "token_hash": repo.hash_token(token),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "refresh_token_expires_at": datetime.now(UTC) + timedelta(days=30),
        "revoked_at": None,
    }
    client = _SequenceAuthClient(
        [
            [session_record],
            {
                "result": [
                    {"status": "OK", "result": []},
                    {"status": "OK", "result": [session_record]},
                ]
            },
        ]
    )
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    result = await repo.mark_current(token)

    assert result is True
    assert len(client.calls) == 2
    assert "ORDER BY created_at" not in " ".join(query for query, _ in client.calls)
    assert client.calls[1][0].count("UPDATE user_sessions") == 2
    assert "is_current = false" in client.calls[1][0]
    assert "is_current = true" in client.calls[1][0]
    assert client.calls[1][1]["user_id"] == str(user_id)
    assert client.calls[1][1]["uuid"] == str(session_id)


@pytest.mark.asyncio
async def test_update_activity_uses_single_conditional_update() -> None:
    token = "access-token"
    repo = surreal_auth_runtime.SurrealSessionRepository(object())
    session_record = {
        "uuid": str(uuid4()),
        "token_hash": repo.hash_token(token),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "revoked_at": None,
    }
    client = _RecordingAuthClient([session_record])
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    result = await repo.update_activity(token)

    assert result is True
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert query.startswith("UPDATE user_sessions SET last_active_at")
    assert "SELECT * FROM user_sessions" not in query
    assert "UPSERT user_sessions" not in query
    assert "expires_at > $now" in query
    assert params["token_hash"] == repo.hash_token(token)
    assert params["last_active_at"] == params["updated_at"] == params["now"]


@pytest.mark.asyncio
async def test_revoke_session_uses_single_conditional_update() -> None:
    user_id = uuid4()
    session_id = uuid4()
    session_record = {
        "uuid": str(session_id),
        "user_id": str(user_id),
        "revoked_at": datetime.now(UTC).replace(tzinfo=None),
    }
    client = _RecordingAuthClient([session_record])
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    result = await repo.revoke_session(session_id, user_id)

    assert result is True
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert query.startswith("UPDATE user_sessions SET revoked_at")
    assert "SELECT * FROM user_sessions" not in query
    assert "UPSERT user_sessions" not in query
    assert "revoked_at = NONE" in query
    assert params["uuid"] == str(session_id)
    assert params["user_id"] == str(user_id)
    assert params["revoked_at"] == params["updated_at"]


@pytest.mark.asyncio
async def test_repository_list_user_sessions_filters_active_rows_in_query() -> None:
    user_id = uuid4()
    session_record = {
        "uuid": str(uuid4()),
        "user_id": str(user_id),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "revoked_at": None,
    }
    client = _RecordingAuthClient([session_record])
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    sessions = await repo.list_user_sessions(user_id)

    assert [session.id for session in sessions] == [UUID(session_record["uuid"])]
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "revoked_at = NONE" in query
    assert "expires_at > $now" in query
    assert params["user_id"] == str(user_id)
    assert "now" in params


@pytest.mark.asyncio
async def test_list_user_sessions_filters_active_rows_in_surreal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    session_id = uuid4()
    client = _RecordingAuthClient(
        [
            {
                "uuid": str(session_id),
                "user_id": str(user_id),
                "token_hash": "hash",
                "expires_at": datetime.now(UTC) + timedelta(minutes=5),
                "last_active_at": datetime.now(UTC).replace(tzinfo=None),
                "created_at": datetime.now(UTC).replace(tzinfo=None),
                "revoked_at": None,
            }
        ]
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )

    sessions = await surreal_auth_runtime.list_user_sessions(user_id=user_id)

    assert [session.id for session in sessions] == [session_id]
    assert sessions[0].token_hash == "hash"
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "revoked_at = NONE" in query
    assert "expires_at > $now" in query
    assert params["user_id"] == str(user_id)
    assert "now" in params


@pytest.mark.asyncio
async def test_revoke_all_sessions_uses_batch_update() -> None:
    user_id = uuid4()
    revoked = [{"uuid": str(uuid4())}, {"uuid": str(uuid4())}]
    client = _RecordingAuthClient(revoked)
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    count = await repo.revoke_all_sessions(user_id, exclude_token_hash="keep-this-session")

    assert count == 2
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert query.startswith("UPDATE user_sessions SET revoked_at = $revoked_at")
    assert "SELECT * FROM user_sessions" not in query
    assert "UPSERT user_sessions" not in query
    assert params["user_id"] == str(user_id)
    assert params["exclude_token_hash"] == "keep-this-session"


@pytest.mark.asyncio
async def test_cleanup_expired_sessions_uses_batch_delete() -> None:
    deleted = [{"uuid": str(uuid4())}, {"uuid": str(uuid4())}]
    client = _RecordingAuthClient(deleted)
    repo = surreal_auth_runtime.SurrealSessionRepository(client)

    count = await repo.cleanup_expired(older_than_days=30)

    assert count == 2
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert query == "DELETE FROM user_sessions WHERE expires_at < $cutoff;"
    assert "cutoff" in params


@pytest.mark.asyncio
async def test_update_auth_user_changes_password_without_repository_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    password_state = hash_password("old-password")
    user_record = {
        "uuid": str(user_id),
        "email": "bliss@example.com",
        "name": "Bliss",
        "password_salt": password_state.salt_hex,
        "password_hash": password_state.hash_hex,
        "password_iterations": password_state.iterations,
    }
    client = _SequenceAuthClient([{"user": user_record, "email_owner": None}])
    written_record: dict[str, object] = {}

    async def replace_record(_self, table, *, uuid, record):
        written_record.update(record)
        return record

    select_one = AsyncMock(side_effect=AssertionError("unexpected user select"))
    audit = AsyncMock()

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealUserRepository,
        "from_client",
        lambda client: (_ for _ in ()).throw(AssertionError("unexpected user repository")),
    )
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "select_one", select_one)
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "replace_record",
        replace_record,
    )
    monkeypatch.setattr(surreal_auth_runtime, "_log_audit_event", audit)

    updated = await surreal_auth_runtime.update_auth_user(
        user_id=user_id,
        email=None,
        name=None,
        avatar_url=None,
        current_password="old-password",
        new_password="new-password",
        organization_id=None,
        request=None,
    )

    assert updated.id == user_id
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "RETURN" in query
    assert "FROM users" in query
    assert "email_owner: NONE" in query
    assert params == {"user_id": str(user_id)}
    select_one.assert_not_awaited()
    assert written_record["password_hash"] != password_state.hash_hex
    assert written_record["password_salt"] != password_state.salt_hex
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_auth_user_rejects_invalid_current_password_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    password_state = hash_password("old-password")
    user_record = {
        "uuid": str(user_id),
        "email": "bliss@example.com",
        "name": "Bliss",
        "password_salt": password_state.salt_hex,
        "password_hash": password_state.hash_hex,
        "password_iterations": password_state.iterations,
    }
    client = _SequenceAuthClient([{"user": user_record, "email_owner": None}])
    replace_record = AsyncMock(side_effect=AssertionError("unexpected user write"))
    audit = AsyncMock(side_effect=AssertionError("unexpected audit log"))

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealUserRepository,
        "from_client",
        lambda client: (_ for _ in ()).throw(AssertionError("unexpected user repository")),
    )
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "select_one",
        AsyncMock(side_effect=AssertionError("unexpected user select")),
    )
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "replace_record",
        replace_record,
    )
    monkeypatch.setattr(surreal_auth_runtime, "_log_audit_event", audit)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_auth_runtime.update_auth_user(
            user_id=user_id,
            email=None,
            name=None,
            avatar_url=None,
            current_password="wrong-password",
            new_password="new-password",
            organization_id=None,
            request=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid current password"
    assert len(client.calls) == 1
    replace_record.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_auth_user_batches_user_and_email_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    owner_id = uuid4()
    client = _SequenceAuthClient(
        [
            {
                "user": {
                    "uuid": str(user_id),
                    "email": "bliss@example.com",
                    "name": "Bliss",
                },
                "email_owner": {
                    "uuid": str(owner_id),
                    "email": "taken@example.com",
                },
            }
        ]
    )
    replace_record = AsyncMock(side_effect=AssertionError("unexpected user write"))
    audit = AsyncMock(side_effect=AssertionError("unexpected audit log"))

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealUserRepository,
        "from_client",
        lambda client: (_ for _ in ()).throw(AssertionError("unexpected user repository")),
    )
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "select_one",
        AsyncMock(side_effect=AssertionError("unexpected user select")),
    )
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "replace_record",
        replace_record,
    )
    monkeypatch.setattr(surreal_auth_runtime, "_log_audit_event", audit)

    with pytest.raises(HTTPException) as exc_info:
        await surreal_auth_runtime.patch_auth_user(
            user_id=user_id,
            updates={"email": "Taken@Example.com"},
            organization_id=None,
            request=None,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Email is already in use"
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "RETURN" in query
    assert "FROM users" in query
    assert "email_owner" in query
    assert params == {"user_id": str(user_id), "email": "taken@example.com"}
    replace_record.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_password_reset_batches_user_and_token_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    user_record = {
        "uuid": str(user_id),
        "email": "bliss@example.com",
        "name": "Bliss",
    }
    existing_tokens = [
        {
            "uuid": str(uuid4()),
            "user_id": str(user_id),
            "created_at": now - timedelta(minutes=10),
            "used_at": None,
            "revoked_at": None,
        },
        {
            "uuid": str(uuid4()),
            "user_id": str(user_id),
            "created_at": now - timedelta(minutes=20),
            "used_at": now - timedelta(minutes=19),
            "revoked_at": None,
        },
    ]
    client = _SequenceAuthClient([{"user": user_record, "tokens": existing_tokens}, [], []])
    replace_record = AsyncMock(side_effect=AssertionError("unexpected per-token write"))
    email_client = SimpleNamespace(send_template=AsyncMock())

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "select_one",
        AsyncMock(side_effect=AssertionError("unexpected split user lookup")),
    )
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "select_many",
        AsyncMock(side_effect=AssertionError("unexpected split token lookup")),
    )
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "replace_record",
        replace_record,
    )
    monkeypatch.setattr(surreal_auth_runtime, "_log_login_history", AsyncMock())
    monkeypatch.setattr(surreal_auth_runtime, "get_email_client", lambda: email_client)

    await surreal_auth_runtime.request_password_reset("Bliss@Example.com")

    queries = [query for query, _params in client.calls]
    read_query, read_params = client.calls[0]
    assert "RETURN" in read_query
    assert "FROM users" in read_query
    assert "FROM password_reset_tokens" in read_query
    assert read_params == {"email": "bliss@example.com"}
    assert any(
        "UPDATE password_reset_tokens SET revoked_at = $revoked_at" in query for query in queries
    )
    assert any("CREATE password_reset_tokens CONTENT $record" in query for query in queries)
    replace_record.assert_not_awaited()
    email_client.send_template.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_project_record_batches_dependent_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id = uuid4()
    project_id = uuid4()
    client = _SequenceAuthClient(
        [
            [
                {
                    "uuid": str(project_id),
                    "organization_id": str(organization_id),
                    "graph_project_id": "project_alpha",
                }
            ],
            {
                "result": [
                    {"status": "OK", "result": []},
                    {"status": "OK", "result": []},
                    {"status": "OK", "result": []},
                    {"status": "OK", "result": []},
                ]
            },
        ]
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )

    deleted = await surreal_auth_runtime.delete_project_record(
        organization_id=organization_id,
        graph_project_id="project_alpha",
    )

    assert deleted is True
    assert len(client.calls) == 2
    delete_query, delete_params = client.calls[1]
    assert delete_query.count("DELETE FROM") == 4
    assert "DELETE FROM api_key_project_scopes" in delete_query
    assert "DELETE FROM team_projects" in delete_query
    assert "DELETE FROM project_members" in delete_query
    assert "DELETE FROM projects" in delete_query
    assert delete_params == {
        "project_id": str(project_id),
        "uuid": str(project_id),
        "organization_id": str(organization_id),
    }


@pytest.mark.asyncio
async def test_confirm_password_reset_batches_token_and_user_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    token_id = uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    token_record = {
        "uuid": str(token_id),
        "user_id": str(user_id),
        "token_hash": surreal_auth_runtime._hash_reset_token("reset-token"),
        "expires_at": now + timedelta(minutes=5),
        "used_at": None,
        "revoked_at": None,
        "created_at": now - timedelta(minutes=1),
    }
    user_record = {
        "uuid": str(user_id),
        "email": "bliss@example.com",
        "name": "Bliss",
    }
    client = _SequenceAuthClient(
        [
            {"token": token_record, "user": user_record},
            {**user_record, "password_hash": "new-hash"},
            {**token_record, "used_at": now},
        ]
    )
    login_history = AsyncMock()

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )
    monkeypatch.setattr(
        surreal_auth_runtime._SurrealRepository,
        "select_one",
        AsyncMock(side_effect=AssertionError("unexpected split reset lookup")),
    )
    monkeypatch.setattr(surreal_auth_runtime, "_log_login_history", login_history)

    await surreal_auth_runtime.confirm_password_reset("reset-token", "new-password")

    assert len(client.calls) == 3
    read_query, read_params = client.calls[0]
    assert "RETURN" in read_query
    assert "FROM password_reset_tokens" in read_query
    assert "FROM users" in read_query
    assert read_params == {"token_hash": surreal_auth_runtime._hash_reset_token("reset-token")}
    assert "UPSERT users CONTENT $record" in client.calls[1][0]
    assert "UPSERT password_reset_tokens CONTENT $record" in client.calls[2][0]
    login_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_user_org_records_batches_organization_reads() -> None:
    user_id = uuid4()
    personal_org_id = uuid4()
    team_org_id = uuid4()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def execute_query(self, query: str, **kwargs: object) -> object:
            self.calls.append((query, kwargs))
            return [
                {
                    "uuid": str(team_org_id),
                    "name": "Team Org",
                    "slug": "team-org",
                    "is_personal": False,
                },
                {
                    "uuid": str(personal_org_id),
                    "name": "Personal Org",
                    "slug": "personal-org",
                    "is_personal": True,
                },
            ]

    client = FakeClient()

    records = await surreal_auth_runtime._list_user_org_records(client, user_id=user_id)

    assert [record["slug"] for record in records] == ["personal-org", "team-org"]
    assert len(client.calls) == 1
    assert "FROM organizations" in client.calls[0][0]
    assert "FROM organization_members" in client.calls[0][0]
    assert client.calls[0][1] == {"user_id": str(user_id)}


@pytest.mark.asyncio
async def test_remove_oauth_connection_batches_safety_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    connection_id = uuid4()
    other_connection_id = uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    connection_record = {
        "uuid": str(connection_id),
        "user_id": str(user_id),
        "provider": "github",
        "provider_user_id": "123",
        "created_at": now,
    }
    client = _SequenceAuthClient(
        [
            {
                "connection": connection_record,
                "user": {"uuid": str(user_id), "password_hash": None},
                "connections": [
                    connection_record,
                    {
                        "uuid": str(other_connection_id),
                        "user_id": str(user_id),
                        "provider": "google",
                        "provider_user_id": "456",
                        "created_at": now,
                    },
                ],
            },
            [],
        ]
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )

    removed = await surreal_auth_runtime.remove_oauth_connection(
        user_id=user_id,
        connection_id=connection_id,
    )

    assert removed.id == connection_id
    assert len(client.calls) == 2
    read_query, read_params = client.calls[0]
    assert "RETURN" in read_query
    assert "FROM oauth_connections" in read_query
    assert "FROM users" in read_query
    assert read_params == {
        "connection_id": str(connection_id),
        "user_id": str(user_id),
    }
    delete_query, delete_params = client.calls[1]
    assert delete_query == "DELETE FROM oauth_connections WHERE uuid = $uuid;"
    assert delete_params == {"uuid": str(connection_id)}


@pytest.mark.asyncio
async def test_surreal_repository_replace_record_rejects_unsupported_tables() -> None:
    repo = surreal_auth_runtime._SurrealRepository(_RecordingAuthClient([]))

    with pytest.raises(ValueError, match="Unsupported replace table: login_history"):
        await repo.replace_record("login_history", uuid=uuid4(), record={"uuid": "login-1"})


@pytest.mark.asyncio
async def test_list_accessible_project_graph_ids_batches_project_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    visible_project_id = uuid4()
    direct_project_id = uuid4()
    team_project_id = uuid4()
    hidden_project_id = uuid4()
    client = _RecordingAuthClient(
        {
            "projects": [
                {
                    "uuid": str(visible_project_id),
                    "graph_project_id": "project_visible",
                    "visibility": "org",
                },
                {
                    "uuid": str(direct_project_id),
                    "graph_project_id": "project_direct",
                    "visibility": "private",
                },
                {
                    "uuid": str(team_project_id),
                    "graph_project_id": "project_team",
                    "visibility": "private",
                },
                {
                    "uuid": str(hidden_project_id),
                    "graph_project_id": "project_hidden",
                    "visibility": "private",
                },
            ],
            "direct_memberships": [{"project_id": str(direct_project_id)}],
            "team_members": [{"team_id": "team_a"}],
            "team_projects": [{"team_id": "team_a", "project_id": str(team_project_id)}],
        }
    )
    ctx = SimpleNamespace(
        organization=SimpleNamespace(id=org_id),
        user=SimpleNamespace(id=user_id),
        org_role="member",
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )

    accessible = await surreal_auth_runtime.list_accessible_project_graph_ids(ctx)

    assert accessible == {"project_visible", "project_direct", "project_team"}
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "RETURN" in query
    assert "FROM projects" in query
    assert "FROM project_members" in query
    assert "FROM team_members" in query
    assert "FROM team_projects" in query
    assert params == {"organization_id": str(org_id), "user_id": str(user_id)}


@pytest.mark.asyncio
async def test_list_accessible_project_graph_ids_admin_skips_grant_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    client = _RecordingAuthClient(
        [
            {"graph_project_id": "project_a"},
            {"graph_project_id": "project_b"},
        ]
    )
    ctx = SimpleNamespace(
        organization=SimpleNamespace(id=org_id),
        user=SimpleNamespace(id=user_id),
        org_role="owner",
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )

    accessible = await surreal_auth_runtime.list_accessible_project_graph_ids(ctx)

    assert accessible == {"project_a", "project_b"}
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "SELECT graph_project_id, created_at FROM projects" in query
    assert "project_members" not in query
    assert "team_members" not in query
    assert "team_projects" not in query
    assert params == {"organization_id": str(org_id)}


@pytest.mark.asyncio
async def test_list_accessible_project_graph_ids_uses_graph_runtime_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.persistence import graph_runtime
    from sibyl_core.models.entities import EntityType

    org_id = uuid4()
    user_id = uuid4()
    client = _RecordingAuthClient([])
    calls: list[dict[str, object]] = []

    class ProjectAdapter:
        async def list_entities_by_type(self, entity_type, **kwargs):
            calls.append({"entity_type": entity_type, **kwargs})
            if len(calls) == 1:
                return [
                    SimpleNamespace(id="project_active", metadata={}),
                    SimpleNamespace(id="project_archived", metadata={"status": "archived"}),
                ]
            return []

    get_adapter = AsyncMock(return_value=ProjectAdapter())
    ctx = SimpleNamespace(
        organization=SimpleNamespace(id=org_id),
        user=SimpleNamespace(id=user_id),
        org_role="owner",
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )
    monkeypatch.setattr(graph_runtime, "get_graph_query_adapter", get_adapter)

    accessible = await surreal_auth_runtime.list_accessible_project_graph_ids(ctx)

    assert accessible == {"project_active"}
    get_adapter.assert_awaited_once_with(str(org_id))
    assert calls == [
        {
            "entity_type": EntityType.PROJECT,
            "limit": 1000,
            "offset": 0,
            "include_archived": True,
        },
        {
            "entity_type": EntityType.PROJECT,
            "limit": 1000,
            "offset": 1000,
            "include_archived": True,
        },
    ]


@pytest.mark.asyncio
async def test_verify_entity_project_access_batches_project_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    project_id = uuid4()
    client = _RecordingAuthClient(
        {
            "project": {
                "uuid": str(project_id),
                "graph_project_id": "project_team",
                "visibility": "private",
            },
            "direct_membership": {
                "project_id": str(project_id),
                "role": ProjectRole.VIEWER.value,
            },
            "team_projects": [
                {"project_id": str(project_id), "role": ProjectRole.MAINTAINER.value}
            ],
        }
    )
    ctx = SimpleNamespace(
        organization=SimpleNamespace(id=org_id),
        user=SimpleNamespace(id=user_id),
        org_role="member",
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )

    role = await surreal_auth_runtime.verify_entity_project_access(
        ctx=ctx,
        entity_project_id="project_team",
        required_role=ProjectRole.CONTRIBUTOR,
    )

    assert role is ProjectRole.MAINTAINER
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "RETURN" in query
    assert "FROM projects" in query
    assert "FROM project_members" in query
    assert "FROM team_members" in query
    assert "FROM team_projects" in query
    assert params == {
        "organization_id": str(org_id),
        "graph_project_id": "project_team",
        "user_id": str(user_id),
    }


@pytest.mark.asyncio
async def test_verify_entity_project_access_admin_skips_project_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = SimpleNamespace(
        organization=SimpleNamespace(id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
        org_role="admin",
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected auth storage")),
    )

    role = await surreal_auth_runtime.verify_entity_project_access(
        ctx=ctx,
        entity_project_id="project_any",
        required_role=ProjectRole.VIEWER,
    )

    assert role is ProjectRole.OWNER


@pytest.mark.asyncio
async def test_verify_entity_project_access_can_require_existing_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    client = _RecordingAuthClient(
        {
            "project": None,
            "direct_membership": None,
            "team_projects": [],
        }
    )
    ctx = SimpleNamespace(
        organization=SimpleNamespace(id=org_id),
        user=SimpleNamespace(id=user_id),
        org_role="member",
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )

    with pytest.raises(HTTPException) as exc:
        await surreal_auth_runtime.verify_entity_project_access(
            ctx=ctx,
            entity_project_id="missing_project",
            required_role=ProjectRole.VIEWER,
            require_existing_project=True,
        )

    assert getattr(exc.value, "status_code", None) == 404
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_start_device_authorization_uses_shared_device_code_primitives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 23, 16, 0, tzinfo=UTC)
    fake_client = SimpleNamespace()
    select_one = AsyncMock(return_value=None)

    async def execute_query(_query: str, **kwargs):
        return [kwargs["record"]]

    fake_client.execute_query = AsyncMock(side_effect=execute_query)

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(fake_client),
    )
    monkeypatch.setattr(surreal_auth_runtime, "_utcnow", lambda: now)
    monkeypatch.setattr(surreal_auth_runtime, "generate_device_code", lambda: "device-secret")
    monkeypatch.setattr(surreal_auth_runtime, "generate_user_code", lambda: "ABCD-EFGH")
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "select_one", select_one)

    req, raw_device_code = await surreal_auth_runtime.start_device_authorization(
        client_name="sibyl-cli",
        scope="mcp",
        expires_in=timedelta(minutes=10),
        poll_interval_seconds=5,
    )

    assert raw_device_code == "device-secret"
    assert req.user_code == "ABCD-EFGH"
    assert req.device_code_hash == surreal_auth_runtime.hash_device_code("device-secret")
    select_one.assert_awaited_once_with(
        "SELECT * FROM device_authorization_requests "
        "WHERE device_code_hash = $device_code_hash OR user_code = $user_code LIMIT 1;",
        device_code_hash=surreal_auth_runtime.hash_device_code("device-secret"),
        user_code="ABCD-EFGH",
    )


@pytest.mark.asyncio
async def test_approve_device_authorization_batches_user_and_request_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 23, 16, 0, tzinfo=UTC).replace(tzinfo=None)
    user_id = uuid4()
    organization_id = uuid4()
    device_request_id = uuid4()
    user_record = {
        "uuid": str(user_id),
        "email": "nova@example.com",
        "name": "Nova",
    }
    request_record = {
        "uuid": str(device_request_id),
        "device_code_hash": "hash",
        "user_code": "ABCD-EFGH",
        "client_name": "sibyl-cli",
        "scope": "mcp",
        "status": "pending",
        "expires_at": now + timedelta(minutes=5),
        "poll_interval_seconds": 5,
    }
    written_record = {
        **request_record,
        "status": "approved",
        "approved_at": now,
        "user_id": str(user_id),
        "organization_id": str(organization_id),
        "updated_at": now,
    }
    organization_record = {
        "uuid": str(organization_id),
        "name": "Nova",
        "slug": f"u-{user_id}",
        "is_personal": True,
        "settings": {},
    }
    membership_record = {
        "uuid": str(uuid4()),
        "organization_id": str(organization_id),
        "user_id": str(user_id),
        "role": "owner",
        "created_at": now,
        "updated_at": now,
    }
    client = _SequenceAuthClient(
        [
            {"user": user_record, "device_request": request_record},
            {"organization": organization_record, "membership": None},
            [membership_record],
            [written_record],
        ]
    )
    audit = AsyncMock()

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )
    monkeypatch.setattr(surreal_auth_runtime, "_utcnow", lambda: now)
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealOrganizationRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected org repository")),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealOrganizationMembershipRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected membership repo")),
    )
    monkeypatch.setattr(surreal_auth_runtime, "_log_audit_event", audit)

    approved = await surreal_auth_runtime.approve_device_authorization(
        user_id=user_id,
        user_code="ABCD-EFGH",
        request=SimpleNamespace(),
    )

    assert approved is not None
    approved_org, approved_request = approved
    assert approved_org.id == organization_id
    assert approved_request.status == "approved"
    assert len(client.calls) == 4
    query, params = client.calls[0]
    assert "RETURN" in query
    assert "FROM users" in query
    assert "FROM device_authorization_requests" in query
    assert "SELECT * FROM users" in query
    assert params == {"user_id": str(user_id), "user_code": "ABCD-EFGH"}
    org_query, org_params = client.calls[1]
    assert "RETURN" in org_query
    assert "FROM organizations" in org_query
    assert "FROM organization_members" in org_query
    assert org_params == {"slug": f"u-{user_id}", "user_id": str(user_id)}
    member_query, member_params = client.calls[2]
    assert "CREATE organization_members CONTENT $record" in member_query
    assert member_params["record"]["organization_id"] == str(organization_id)
    assert member_params["record"]["user_id"] == str(user_id)
    assert member_params["record"]["role"] == "owner"
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_has_owner_membership_uses_direct_membership_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    user_id = uuid4()
    client = _RecordingAuthClient([{"role": "owner"}])

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(client),
    )
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealOrganizationMembershipRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected membership repo")),
    )

    result = await surreal_auth_runtime.has_owner_membership(
        org_id=str(org_id),
        user_id=str(user_id),
    )

    assert result is True
    assert len(client.calls) == 1
    query, params = client.calls[0]
    assert "FROM organization_members" in query
    assert params == {"organization_id": str(org_id), "user_id": str(user_id)}


@pytest.mark.asyncio
async def test_exchange_device_code_accepts_aware_datetime_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_request_id = uuid4()
    user_id = uuid4()
    organization_id = uuid4()
    fake_client = _RecordingAuthClient([{"uuid": str(device_request_id)}])
    create_session = AsyncMock()
    replace_record = AsyncMock()
    access_token_kwargs: dict[str, object] = {}
    refresh_token_kwargs: dict[str, object] = {}
    select_one = AsyncMock(
        return_value={
            "uuid": str(device_request_id),
            "device_code_hash": "hash",
            "user_code": "ABCD-EFGH",
            "client_name": "sibyl-cli",
            "scope": "mcp",
            "status": "approved",
            "poll_interval_seconds": 5,
            "last_polled_at": datetime.now(UTC) - timedelta(seconds=10),
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "approved_at": datetime.now(UTC) - timedelta(seconds=5),
            "denied_at": None,
            "consumed_at": None,
            "user_id": str(user_id),
            "organization_id": str(organization_id),
            "created_at": datetime.now(UTC) - timedelta(minutes=1),
            "updated_at": datetime.now(UTC) - timedelta(seconds=10),
        }
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(fake_client),
    )
    monkeypatch.setattr(
        surreal_auth_runtime,
        "create_access_token",
        lambda **kwargs: access_token_kwargs.update(kwargs) or "access-token",
    )
    monkeypatch.setattr(
        surreal_auth_runtime,
        "create_refresh_token",
        lambda **kwargs: refresh_token_kwargs.update(kwargs)
        or ("refresh-token", datetime.now(UTC) + timedelta(days=30)),
    )
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "select_one", select_one)
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "replace_record", replace_record)
    monkeypatch.setattr(
        surreal_auth_runtime.SurrealSessionRepository,
        "from_client",
        lambda client: SimpleNamespace(create_session=create_session),
    )

    token = await surreal_auth_runtime.exchange_device_code(device_code="device-code")

    assert token["access_token"] == "access-token"
    assert token["refresh_token"] == "refresh-token"
    create_session.assert_awaited_once()
    session_kwargs = create_session.await_args.kwargs
    assert session_kwargs["session_id"] == access_token_kwargs["session_id"]
    assert session_kwargs["session_id"] == refresh_token_kwargs["session_id"]
    replace_record.assert_not_awaited()
    assert len(fake_client.calls) == 1
    query, params = fake_client.calls[0]
    assert query.lstrip().startswith("UPDATE device_authorization_requests")
    assert "UPSERT device_authorization_requests" not in query
    assert params["uuid"] == str(device_request_id)
    assert params["status"] == "consumed"


@pytest.mark.asyncio
async def test_exchange_device_code_handles_missing_last_polled_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_request_id = uuid4()
    fake_client = _RecordingAuthClient([{"uuid": str(device_request_id)}])
    replace_record = AsyncMock()
    select_one = AsyncMock(
        return_value={
            "uuid": str(device_request_id),
            "device_code_hash": "hash",
            "user_code": "ABCD-EFGH",
            "client_name": "sibyl-cli",
            "scope": "mcp",
            "status": "pending",
            "poll_interval_seconds": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "created_at": datetime.now(UTC) - timedelta(minutes=1),
            "updated_at": datetime.now(UTC) - timedelta(seconds=10),
        }
    )

    monkeypatch.setattr(
        surreal_auth_runtime,
        "_auth_client_scope",
        lambda: _StaticAuthClientScope(fake_client),
    )
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "select_one", select_one)
    monkeypatch.setattr(surreal_auth_runtime._SurrealRepository, "replace_record", replace_record)

    with pytest.raises(DeviceTokenError, match="Authorization pending") as exc_info:
        await surreal_auth_runtime.exchange_device_code(device_code="device-code")

    assert exc_info.value.error == "authorization_pending"
    replace_record.assert_not_awaited()
    assert len(fake_client.calls) == 1
    query, params = fake_client.calls[0]
    assert query.lstrip().startswith("UPDATE device_authorization_requests")
    assert "UPSERT device_authorization_requests" not in query
    assert params["uuid"] == str(device_request_id)
    assert params["last_polled_at"] == params["updated_at"]
