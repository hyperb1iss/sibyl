from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Request
from pydantic import SecretStr

from sibyl import config
from sibyl.api.routes import archive_import_authority as authority
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.dependencies import _api_key_claims
from sibyl.auth.jwt import create_access_token
from sibyl.auth.session_cache import access_session_cache
from sibyl.persistence.surreal import auth_runtime
from sibyl.persistence.surreal.auth_runtime import api_keys
from sibyl_core.auth import AuthContext, OrganizationRole
from sibyl_core.backends.surreal import SurrealAuthClient
from sibyl_core.backends.surreal.auth_schema import bootstrap_auth_schema
from sibyl_core.migrate.personal_archive_plan import ArchiveCredentialCeiling


@dataclass
class ArchiveAuthFixture:
    client: SurrealAuthClient
    organization_id: UUID
    user_id: UUID
    private_space_id: UUID

    async def key(self, *, projects=None, spaces=None, scopes=None):
        return await api_keys.create_api_key_for_user(
            organization_id=self.organization_id,
            user_id=self.user_id,
            name="archive-test-key",
            live=False,
            scopes=["api:write"] if scopes is None else scopes,
            project_ids=projects,
            memory_space_ids=spaces,
            expires_at=None,
            request=None,
        )

    async def key_context(self, token: str) -> AuthContext:
        presented = await api_keys.authenticate_api_key(token)
        assert presented is not None
        return await auth_runtime.resolve_auth_context(
            claims=_api_key_claims(presented, scopes=list(presented.scopes))
        )

    async def session(self):
        session_id = uuid4()
        token = create_access_token(
            user_id=self.user_id,
            organization_id=self.organization_id,
            session_id=session_id,
        )
        repository = auth_runtime.SessionRepository.from_client(self.client)
        session = await repository.create_session(
            user_id=self.user_id,
            organization_id=self.organization_id,
            session_id=session_id,
            token=token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        context = await auth_runtime.resolve_auth_context(
            claims=authority.verify_access_token(token)
        )
        return session, token, context


def _request(token: str | None, context: AuthContext, *, method="POST", cookie=False):
    headers = []
    if token:
        header = (
            (b"cookie", ("sibyl_access_token=" + token).encode())
            if cookie
            else (b"authorization", ("Bearer " + token).encode())
        )
        headers.append(header)
    request = Request(
        {"type": "http", "method": method, "path": "/api/archive-imports/check", "headers": headers}
    )
    request.state.auth_context = context
    request.state.validated_auth_claims = {"sub": context.user_id, "org": context.organization_id}
    request.state.jwt_claims = request.state.validated_auth_claims
    return request


@pytest.fixture
async def archive_auth(monkeypatch):
    namespace = "archive_auth_" + uuid4().hex
    client = SurrealAuthClient(
        url=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_AUTH_TEST_PASSWORD", ""),
        namespace=namespace,
    )
    await client.connect()
    await bootstrap_auth_schema(client)

    @asynccontextmanager
    async def scope():
        yield client

    monkeypatch.setattr(auth_runtime, "_auth_client_scope", scope)
    monkeypatch.setattr(authority, "surreal_auth_client_scope", scope)
    monkeypatch.setattr(config.settings, "jwt_secret", SecretStr("archive-test-secret-" + "x" * 64))
    org, user, space = uuid4(), uuid4(), uuid4()
    await client.execute_query(
        "CREATE users CONTENT $user; CREATE organizations CONTENT $org; "
        "CREATE organization_members CONTENT $membership; CREATE memory_spaces CONTENT $space;",
        user={"uuid": str(user), "email": "archive@example.test", "name": "Archive fixture"},
        org={"uuid": str(org), "name": "Archive fixture", "slug": "archive-fixture"},
        membership={
            "uuid": str(uuid4()),
            "organization_id": str(org),
            "user_id": str(user),
            "role": "member",
        },
        space={
            "uuid": str(space),
            "organization_id": str(org),
            "memory_scope": "private",
            "scope_key": str(user),
            "created_by_user_id": str(user),
        },
    )
    for project in ("project-a", "project-b"):
        await client.execute_query(
            "CREATE projects CONTENT $row;",
            row={
                "uuid": str(uuid4()),
                "organization_id": str(org),
                "graph_project_id": project,
                "name": project,
                "slug": project,
            },
        )
    try:
        yield ArchiveAuthFixture(client, org, user, space)
    finally:
        try:
            await client.execute_query(f"REMOVE NAMESPACE `{namespace}`;")
        finally:
            await client.close()


async def test_archive_authority_native_healthy_restricted_key(archive_auth):
    fixture = archive_auth
    key, token = await fixture.key(projects=["project-a"], spaces=[fixture.private_space_id])
    initial = await fixture.key_context(token)
    refreshed, ceiling = await authority.refresh_archive_authority(
        _request(token, initial), initial
    )
    assert refreshed.api_key_id == str(key.id)
    assert refreshed.api_key_project_ids == frozenset({"project-a"})
    assert refreshed.api_key_memory_space_ids == frozenset({str(fixture.private_space_id)})
    assert refreshed.api_key_memory_scope_keys == frozenset(
        {api_key_memory_scope_key("private", str(fixture.user_id))}
    )
    assert refreshed.org_role == OrganizationRole.MEMBER
    assert ceiling.project_restricted
    assert ceiling.memory_restricted


@pytest.mark.parametrize("change", ["revoked", "expired"])
async def test_archive_authority_native_key_change_defeats_request_cache(archive_auth, change):
    fixture = archive_auth
    key, token = await fixture.key()
    initial = await fixture.key_context(token)
    query = (
        "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$id;"
        if change == "revoked"
        else "UPDATE api_keys SET expires_at=time::now()-1h WHERE uuid=$id;"
    )
    await fixture.client.execute_query(query, id=str(key.id))
    with pytest.raises(HTTPException) as rejected:
        await authority.refresh_archive_authority(_request(token, initial), initial)
    assert rejected.value.status_code == 401


async def test_archive_authority_native_rest_downgrade_preserves_read(archive_auth):
    fixture = archive_auth
    key, token = await fixture.key()
    initial = await fixture.key_context(token)
    original = authority.archive_credential_ceiling(initial)
    await fixture.client.execute_query(
        "UPDATE api_keys SET scopes=['api:read'] WHERE uuid=$id;", id=str(key.id)
    )
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(
            _request(token, initial), initial, original_ceiling=original
        )
    assert denied.value.status_code == 403
    fresh, ceiling = await authority.refresh_archive_authority(
        _request(token, initial, method="GET"), initial, original_ceiling=original
    )
    assert fresh.scopes == frozenset({"api:read"})
    assert ceiling.rest_scopes == ("api:read",)


@pytest.mark.parametrize("change", ["viewer", "removed"])
async def test_archive_authority_native_membership_changes_defeat_cache(archive_auth, change):
    fixture = archive_auth
    _, token = await fixture.key()
    initial = await fixture.key_context(token)
    await fixture.client.execute_query(
        "UPDATE organization_members SET role='viewer' WHERE user_id=$id;"
        if change == "viewer"
        else "DELETE organization_members WHERE user_id=$id;",
        id=str(fixture.user_id),
    )
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(_request(token, initial), initial)
    assert denied.value.status_code == 403
    if change == "viewer":
        fresh, _ = await authority.refresh_archive_authority(
            _request(token, initial, method="GET"), initial
        )
        assert fresh.org_role == OrganizationRole.VIEWER
    else:
        with pytest.raises(HTTPException) as denied_get:
            await authority.refresh_archive_authority(
                _request(token, initial, method="GET"), initial
            )
        assert denied_get.value.status_code == 403


async def test_archive_authority_native_stronger_retry_retains_original_ceiling(archive_auth):
    fixture = archive_auth
    key, token = await fixture.key(projects=["project-a"], spaces=[fixture.private_space_id])
    original = authority.archive_credential_ceiling(await fixture.key_context(token))
    await fixture.client.execute_query(
        "DELETE api_key_project_scopes WHERE api_key_id=$id; "
        "DELETE api_key_memory_space_scopes WHERE api_key_id=$id; "
        "UPDATE api_keys SET project_scope_restricted=false, memory_scope_restricted=false WHERE uuid=$id;",
        id=str(key.id),
    )
    retry_context = await fixture.key_context(token)
    assert retry_context.api_key_project_ids is None
    assert retry_context.api_key_memory_scope_keys is None
    fresh, ceiling = await authority.refresh_archive_authority(
        _request(token, retry_context), retry_context, original_ceiling=original
    )
    assert fresh.api_key_project_ids == frozenset({"project-a"})
    assert fresh.api_key_memory_space_ids == frozenset({str(fixture.private_space_id)})
    assert fresh.api_key_memory_scope_keys == frozenset(
        {api_key_memory_scope_key("private", str(fixture.user_id))}
    )
    assert ceiling.project_restricted
    assert ceiling.memory_restricted


@pytest.mark.parametrize("kind", ["project", "memory"])
async def test_archive_authority_native_restricted_empty_stays_empty(archive_auth, kind):
    fixture = archive_auth
    _, token = await fixture.key()
    initial = await fixture.key_context(token)
    original = ArchiveCredentialCeiling(
        credential_kind="api_key",
        api_key_id=initial.api_key_id,
        rest_scopes=("api:write",),
        project_restricted=kind == "project",
        memory_restricted=kind == "memory",
    )
    fresh, _ = await authority.refresh_archive_authority(
        _request(token, initial), initial, original_ceiling=original
    )
    if kind == "project":
        assert fresh.api_key_project_ids == frozenset()
    else:
        assert fresh.api_key_memory_space_ids == frozenset()
        assert fresh.api_key_memory_scope_keys == frozenset()


async def test_archive_authority_native_request_start_ceiling_also_limits_retry(archive_auth):
    fixture = archive_auth
    key, token = await fixture.key(projects=["project-a", "project-b"])
    original = authority.archive_credential_ceiling(await fixture.key_context(token))
    await fixture.client.execute_query(
        "DELETE api_key_project_scopes WHERE api_key_id=$key AND project_id IN "
        "(SELECT VALUE uuid FROM projects WHERE graph_project_id='project-b');",
        key=str(key.id),
    )
    initial = await fixture.key_context(token)
    assert initial.api_key_project_ids == frozenset({"project-a"})
    await fixture.client.execute_query(
        "DELETE api_key_project_scopes WHERE api_key_id=$key; "
        "UPDATE api_keys SET project_scope_restricted=false WHERE uuid=$key;",
        key=str(key.id),
    )
    fresh, _ = await authority.refresh_archive_authority(
        _request(token, initial), initial, original_ceiling=original
    )
    assert fresh.api_key_project_ids == frozenset({"project-a"})


@pytest.mark.parametrize("stage", ["request", "replay"])
async def test_archive_authority_native_changed_key_identity_conflicts(archive_auth, stage):
    fixture = archive_auth
    _, first_token = await fixture.key()
    _, second_token = await fixture.key()
    first = await fixture.key_context(first_token)
    second = await fixture.key_context(second_token)
    initial = first if stage == "request" else second
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(
            _request(second_token, initial),
            initial,
            original_ceiling=authority.archive_credential_ceiling(first),
        )
    assert denied.value.status_code == 409


async def test_archive_authority_native_revoked_session_ignores_positive_cache(archive_auth):
    fixture = archive_auth
    session, token, initial = await fixture.session()
    assert access_session_cache.get(session.id) is True
    await fixture.client.execute_query(
        "UPDATE user_sessions SET revoked_at=time::now() WHERE uuid=$id;", id=str(session.id)
    )
    assert access_session_cache.get(session.id) is True
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(_request(token, initial, cookie=True), initial)
    assert denied.value.status_code == 401


async def test_archive_authority_native_session_current_role_and_team(archive_auth):
    fixture = archive_auth
    team = uuid4()
    await fixture.client.execute_query(
        "CREATE teams CONTENT $team; CREATE team_members CONTENT $member;",
        team={
            "uuid": str(team),
            "organization_id": str(fixture.organization_id),
            "name": "Archive team",
            "slug": "archive-team",
        },
        member={
            "uuid": str(uuid4()),
            "team_id": str(team),
            "user_id": str(fixture.user_id),
            "role": "member",
        },
    )
    _, token, initial = await fixture.session()
    assert initial.accessible_teams == frozenset({str(team)})
    await fixture.client.execute_query(
        "DELETE team_members WHERE user_id=$id;", id=str(fixture.user_id)
    )
    fresh, _ = await authority.refresh_archive_authority(_request(token, initial), initial)
    assert fresh.accessible_teams == frozenset()
    await fixture.client.execute_query(
        "UPDATE organization_members SET role='viewer' WHERE user_id=$id;", id=str(fixture.user_id)
    )
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(_request(token, initial), initial)
    assert denied.value.status_code == 403


@pytest.mark.parametrize("field", ["user_id", "organization_id"])
async def test_archive_authority_native_session_record_matches_signed_claims(archive_auth, field):
    fixture = archive_auth
    session, token, initial = await fixture.session()
    query = {
        "user_id": "UPDATE user_sessions SET user_id=$value WHERE uuid=$id;",
        "organization_id": "UPDATE user_sessions SET organization_id=$value WHERE uuid=$id;",
    }[field]
    await fixture.client.execute_query(query, value=str(uuid4()), id=str(session.id))
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(_request(token, initial), initial)
    assert denied.value.status_code == 401


async def test_archive_authority_native_rotated_session_preserves_existing_sid_policy(archive_auth):
    fixture = archive_auth
    session, token, initial = await fixture.session()
    replacement = create_access_token(
        user_id=fixture.user_id,
        organization_id=fixture.organization_id,
        session_id=session.id,
        extra_claims={"rotation": uuid4().hex},
    )
    repository = auth_runtime.SessionRepository.from_client(fixture.client)
    await repository.rotate_tokens(
        session,
        new_access_token=replacement,
        new_access_expires_at=datetime.now(UTC) + timedelta(hours=1),
        new_refresh_token=uuid4().hex,
        new_refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    assert await repository.get_session_by_token(token) is None
    fresh, _ = await authority.refresh_archive_authority(_request(token, initial), initial)
    assert fresh.user_id == initial.user_id


@pytest.mark.parametrize("token", [None, "invalid-presented-token"])
async def test_archive_authority_cached_claims_cannot_replace_credentials(archive_auth, token):
    _, _, initial = await archive_auth.session()
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(_request(token, initial), initial)
    assert denied.value.status_code == 401


async def test_archive_authority_native_revocation_after_presented_key_verification(
    archive_auth, monkeypatch
):
    fixture = archive_auth
    key, token = await fixture.key()
    initial = await fixture.key_context(token)
    authenticate = authority.authenticate_api_key

    async def revoke_after_verification(presented):
        verified = await authenticate(presented)
        assert verified is not None
        await fixture.client.execute_query(
            "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$id;", id=str(key.id)
        )
        return verified

    monkeypatch.setattr(authority, "authenticate_api_key", revoke_after_verification)
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(_request(token, initial), initial)
    assert denied.value.status_code == 401


@pytest.mark.parametrize("target", ["project", "memory"])
async def test_archive_authority_native_deleted_scoped_target_remains_restricted(
    archive_auth, target
):
    fixture = archive_auth
    _, token = await fixture.key(projects=["project-a"], spaces=[fixture.private_space_id])
    initial = await fixture.key_context(token)
    await fixture.client.execute_query(
        "DELETE projects WHERE graph_project_id='project-a';"
        if target == "project"
        else "DELETE memory_spaces WHERE uuid=$id;",
        id=str(fixture.private_space_id),
    )
    fresh, ceiling = await authority.refresh_archive_authority(_request(token, initial), initial)
    if target == "project":
        assert ceiling.project_restricted
        assert fresh.api_key_project_ids == frozenset()
    else:
        assert ceiling.memory_restricted
        assert fresh.api_key_memory_space_ids == frozenset()
        assert fresh.api_key_memory_scope_keys == frozenset()


async def test_archive_authority_auth_timeout_is_unavailable(archive_auth, monkeypatch):
    _, token = await archive_auth.key()
    initial = await archive_auth.key_context(token)

    async def unavailable(_token):
        raise TimeoutError("synthetic auth store unavailable")

    monkeypatch.setattr(authority, "authenticate_api_key", unavailable)
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(_request(token, initial), initial)
    assert denied.value.status_code == 503
    assert denied.value.detail == "Authentication storage temporarily unavailable"


async def test_archive_authority_native_legacy_session_without_sid(archive_auth):
    fixture = archive_auth
    token = create_access_token(user_id=fixture.user_id, organization_id=fixture.organization_id)
    repository = auth_runtime.SessionRepository.from_client(fixture.client)
    await repository.create_session(
        user_id=fixture.user_id,
        organization_id=fixture.organization_id,
        token=token,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    initial = await auth_runtime.resolve_auth_context(claims=authority.verify_access_token(token))
    fresh, ceiling = await authority.refresh_archive_authority(_request(token, initial), initial)
    assert fresh.user_id == initial.user_id
    assert ceiling.credential_kind == "session"


async def test_archive_authority_bearer_precedence_does_not_adopt_cookie(archive_auth):
    _, token, initial = await archive_auth.session()
    request = _request("invalid-presented-token", initial)
    request.scope["headers"].append((b"cookie", ("sibyl_access_token=" + token).encode()))
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(request, initial)
    assert denied.value.status_code == 401


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_archive_authority_native_empty_rest_scopes_do_not_gain_legacy_mcp_access(
    archive_auth, method
):
    fixture = archive_auth
    key, token = await fixture.key()
    initial = await fixture.key_context(token)
    await fixture.client.execute_query(
        "UPDATE api_keys SET scopes=[] WHERE uuid=$id;", id=str(key.id)
    )
    with pytest.raises(HTTPException) as denied:
        await authority.refresh_archive_authority(_request(token, initial, method=method), initial)
    assert denied.value.status_code == 403
