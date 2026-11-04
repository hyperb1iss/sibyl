"""Bot accounts against a live SurrealDB server.

Each test bootstraps the auth schema in a namespace of its own and points the
shared auth client at it, so every runtime function under test issues its real
queries: the resolver, key authentication, the project grant snapshots and the
cap, the bot routes' runtime, and the member and team mutations.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.authorization import ProjectAuthorizationError
from sibyl.auth.dependencies import _api_key_claims
from sibyl.persistence.auth_common import SuspendedPrincipalError
from sibyl.persistence.surreal import auth as surreal_auth, organization_runtime
from sibyl.persistence.surreal.auth_runtime import (
    _common as auth_common,
    api_keys as key_runtime,
    audit as audit_runtime,
    bots as bot_runtime,
    projects as project_runtime,
    sessions as session_runtime,
    users as user_runtime,
)
from sibyl_core.auth import AuthContext, OrganizationRole, PrincipalKind, ProjectRole
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema
from sibyl_core.backends.surreal.records import normalize_records, utcnow
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url

pytestmark = pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1",
    reason="live SurrealDB runtime smoke tests are disabled",
)

BOT_KEY_SCOPES = ["api:read", "api:write", "mcp"]


def _live_surreal_url() -> str:
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


def _credentials() -> dict[str, str]:
    return {
        "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
        "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
    }


async def _drop_namespace(namespace: str) -> None:
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(_live_surreal_url())
    try:
        await client.signin(_credentials())
        await client.query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
    finally:
        await client.close()


@asynccontextmanager
async def _live_auth_store() -> AsyncIterator[SurrealAuthClient]:
    namespace = f"bot_accounts_{uuid4().hex}"
    client = SurrealAuthClient(
        url=_live_surreal_url(), namespace=namespace, database="auth", **_credentials()
    )
    await bootstrap_auth_schema(client)
    previous = surreal_auth._shared_auth_client_state.client
    surreal_auth._shared_auth_client_state.client = client
    try:
        yield client
    finally:
        surreal_auth._shared_auth_client_state.client = previous
        await client.close()
        await _drop_namespace(namespace)


async def _create(client: SurrealAuthClient, table: str, record: dict[str, object]) -> None:
    created = normalize_records(
        await client.execute_query(f"CREATE {table} CONTENT $record;", record=record)
    )
    assert created, f"seed {table} failed"


async def _seed(client: SurrealAuthClient) -> SimpleNamespace:
    """One organization: an owner, a sponsor, another member and a viewer.

    P1 is private and the sponsor contributes; P2 is org-visible with a viewer
    default; P3 is private and the sponsor only views.
    """
    now = utcnow()
    org_id = uuid4()
    world = SimpleNamespace(
        org_id=org_id,
        slug=f"bots-{org_id.hex[:10]}",
        owner=uuid4(),
        sponsor=uuid4(),
        member=uuid4(),
        viewer=uuid4(),
        p1=f"project_{uuid4().hex[:12]}",
        p2=f"project_{uuid4().hex[:12]}",
        p3=f"project_{uuid4().hex[:12]}",
        p1_uuid=uuid4(),
        p2_uuid=uuid4(),
        p3_uuid=uuid4(),
    )
    await _create(
        client,
        "organizations",
        {"uuid": str(org_id), "name": "Bots", "slug": world.slug, "is_personal": False},
    )
    for label, user_id, role in (
        ("owner", world.owner, "owner"),
        ("sponsor", world.sponsor, "member"),
        ("member", world.member, "member"),
        ("viewer", world.viewer, "viewer"),
    ):
        await _create(
            client,
            "users",
            {
                "uuid": str(user_id),
                "email": f"{label}-{org_id.hex[:8]}@example.test",
                "name": label.title(),
                "created_at": now,
                "updated_at": now,
            },
        )
        await _create(
            client,
            "organization_members",
            {
                "uuid": str(uuid4()),
                "organization_id": str(org_id),
                "user_id": str(user_id),
                "role": role,
            },
        )
    for graph_id, project_uuid, visibility in (
        (world.p1, world.p1_uuid, "private"),
        (world.p2, world.p2_uuid, "org"),
        (world.p3, world.p3_uuid, "private"),
    ):
        await _create(
            client,
            "projects",
            {
                "uuid": str(project_uuid),
                "organization_id": str(org_id),
                "name": graph_id,
                "slug": graph_id,
                "graph_project_id": graph_id,
                "visibility": visibility,
                "default_role": "project_viewer",
                "owner_user_id": str(world.owner),
                "created_at": now,
            },
        )
    for project_uuid, role in (
        (world.p1_uuid, "project_contributor"),
        (world.p3_uuid, "project_viewer"),
    ):
        await _create(
            client,
            "project_members",
            {
                "uuid": str(uuid4()),
                "organization_id": str(org_id),
                "project_id": str(project_uuid),
                "user_id": str(world.sponsor),
                "role": role,
            },
        )
    return world


async def _ctx(world: SimpleNamespace, user_id: UUID) -> AuthContext:
    return await session_runtime.resolve_auth_context(
        claims={"sub": str(user_id), "org": str(world.org_id), "scopes": []}
    )


async def _bot_ctx(raw_key: str) -> AuthContext:
    auth = await key_runtime.authenticate_api_key(raw_key)
    assert isinstance(auth, ApiKeyAuth), "the bot key no longer authenticates"
    return await session_runtime.resolve_auth_context(
        claims=_api_key_claims(auth, scopes=list(auth.scopes or []))
    )


async def _writable(ctx: AuthContext) -> set[str]:
    return await project_runtime.list_accessible_project_graph_ids(
        ctx, required_role=ProjectRole.CONTRIBUTOR
    )


async def _audit(client: SurrealAuthClient, action: str) -> list[dict[str, object]]:
    return normalize_records(
        await client.execute_query(
            "SELECT * FROM audit_logs WHERE action = $action ORDER BY created_at ASC;",
            action=action,
        )
    )


async def _issue_key(ctx: AuthContext, bot_id: UUID) -> str:
    _key, raw = await bot_runtime.create_bot_api_key(
        ctx=ctx,
        bot_id=bot_id,
        name="pager",
        live=True,
        scopes=BOT_KEY_SCOPES,
        project_ids=None,
        memory_space_ids=None,
        expires_at=None,
        request=None,
    )
    return raw


@pytest.mark.asyncio
async def test_live_a_bot_lives_inside_its_sponsor_s_access(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot_runtime, "soft_delete_private_raw_captures_for_user", AsyncMock(return_value=0)
    )
    async with _live_auth_store() as client:
        world = await _seed(client)
        sponsor = await _ctx(world, world.sponsor)
        owner = await _ctx(world, world.owner)
        member = await _ctx(world, world.member)
        viewer = await _ctx(world, world.viewer)

        # Creation: any non-viewer, for themselves.
        with pytest.raises(HTTPException) as refused:
            await bot_runtime.create_bot(ctx=viewer, name="nope", request=None)
        assert refused.value.status_code == 403
        bot = await bot_runtime.create_bot(ctx=sponsor, name="oncall", request=None)
        bot_id = UUID(str(bot["id"]))
        assert bot["status"] == "active"
        assert bot["sponsor"]["id"] == str(world.sponsor)
        [created] = await _audit(client, "bot.create")
        assert created["user_id"] == str(world.sponsor)
        assert created["details"]["bot_user_id"] == str(bot_id)

        # Grants: at most min(sponsor's role, contributor), from the sponsor only.
        await bot_runtime.grant_bot_project(
            ctx=sponsor,
            bot_id=bot_id,
            project_ref=world.p1,
            role=ProjectRole.CONTRIBUTOR,
            request=None,
        )
        with pytest.raises(HTTPException) as refused:
            await bot_runtime.grant_bot_project(
                ctx=sponsor,
                bot_id=bot_id,
                project_ref=world.p3,
                role=ProjectRole.CONTRIBUTOR,
                request=None,
            )
        assert refused.value.status_code == 403
        assert refused.value.detail["error"] == "bot_grant_exceeds_sponsor"
        await bot_runtime.grant_bot_project(
            ctx=sponsor,
            bot_id=bot_id,
            project_ref=str(world.p3_uuid),
            role=ProjectRole.VIEWER,
            request=None,
        )
        with pytest.raises(HTTPException) as refused:
            await bot_runtime.grant_bot_project(
                ctx=owner,
                bot_id=bot_id,
                project_ref=world.p2,
                role=ProjectRole.VIEWER,
                request=None,
            )
        assert refused.value.detail["error"] == "bot_sponsor_required"
        with pytest.raises(HTTPException) as refused:
            await bot_runtime.grant_bot_project(
                ctx=member,
                bot_id=bot_id,
                project_ref=world.p2,
                role=ProjectRole.VIEWER,
                request=None,
            )
        assert refused.value.status_code == 404
        grants = await _audit(client, "bot.project.grant")
        assert [grant["details"]["project_id"] for grant in grants] == [world.p1, world.p3]
        assert {grant["user_id"] for grant in grants} == {str(world.sponsor)}

        shown = await bot_runtime.get_bot(ctx=owner, bot_id=bot_id)
        assert {(grant["project_id"], grant["role"]) for grant in shown["projects"]} == {
            (world.p1, "project_contributor"),
            (world.p3, "project_viewer"),
        }
        with pytest.raises(HTTPException):
            await bot_runtime.get_bot(ctx=member, bot_id=bot_id)

        # Keys: issued by the sponsor, audited under the sponsor.
        raw = await _issue_key(sponsor, bot_id)
        [issued] = await _audit(client, "auth.api_key.create")
        assert issued["user_id"] == str(world.sponsor)
        assert issued["details"]["owner_user_id"] == str(bot_id)
        [listed_key] = await bot_runtime.list_bot_api_keys(ctx=owner, bot_id=bot_id)
        assert listed_key.user_id == bot_id

        bot_ctx = await _bot_ctx(raw)
        assert bot_ctx.user.kind is PrincipalKind.SERVICE
        assert bot_ctx.org_role is OrganizationRole.MEMBER
        assert bot_ctx.sponsor_org_role is OrganizationRole.MEMBER
        readable = await project_runtime.list_accessible_project_graph_ids(bot_ctx)
        assert readable == {world.p1, world.p2, world.p3}
        assert await _writable(bot_ctx) == {world.p1}
        assert (
            await project_runtime.verify_entity_project_access(
                ctx=bot_ctx, entity_project_id=world.p1, required_role=ProjectRole.CONTRIBUTOR
            )
            is ProjectRole.CONTRIBUTOR
        )

        # The cap reaches org-visibility defaults: a maintainer default on P2
        # lands at contributor for the bot, never maintainer.
        await client.execute_query(
            "UPDATE projects SET default_role = 'project_maintainer' WHERE uuid = $uuid;",
            uuid=str(world.p2_uuid),
        )
        bot_ctx = await _bot_ctx(raw)
        assert (
            await project_runtime.verify_entity_project_access(
                ctx=bot_ctx, entity_project_id=world.p2, required_role=ProjectRole.VIEWER
            )
            is ProjectRole.CONTRIBUTOR
        )
        with pytest.raises(ProjectAuthorizationError):
            await project_runtime.verify_entity_project_access(
                ctx=bot_ctx, entity_project_id=world.p2, required_role=ProjectRole.MAINTAINER
            )
        await client.execute_query(
            "UPDATE projects SET default_role = 'project_viewer' WHERE uuid = $uuid;",
            uuid=str(world.p2_uuid),
        )

        # The sponsor drops to viewer on P1: the bot drops on its next request.
        await client.execute_query(
            "UPDATE project_members SET role = 'project_viewer' "
            "WHERE project_id = $project_id AND user_id = $user_id;",
            project_id=str(world.p1_uuid),
            user_id=str(world.sponsor),
        )
        bot_ctx = await _bot_ctx(raw)
        assert await _writable(bot_ctx) == set()
        with pytest.raises(ProjectAuthorizationError):
            await project_runtime.verify_entity_project_access(
                ctx=bot_ctx, entity_project_id=world.p1, required_role=ProjectRole.CONTRIBUTOR
            )
        assert (
            await project_runtime.verify_entity_project_access(
                ctx=bot_ctx, entity_project_id=world.p1, required_role=ProjectRole.VIEWER
            )
            is ProjectRole.VIEWER
        )
        readable, writable = await project_runtime.resolve_project_graph_grants(
            user_id=str(bot_id), org_id=str(world.org_id)
        )
        assert writable == frozenset()
        assert world.p1 in readable

        # The sponsor leaves the organization: every bot key is refused.
        await client.execute_query(
            "DELETE FROM organization_members WHERE organization_id = $org AND user_id = $user;",
            org=str(world.org_id),
            user=str(world.sponsor),
        )
        with pytest.raises(SuspendedPrincipalError):
            await _bot_ctx(raw)
        assert await project_runtime.resolve_project_graph_grants(
            user_id=str(bot_id), org_id=str(world.org_id)
        ) == (frozenset(), frozenset())
        [suspended] = await bot_runtime.list_bots(ctx=owner)
        assert suspended["status"] == "suspended"
        with pytest.raises(HTTPException) as refused:
            await _issue_key(owner, bot_id)
        assert refused.value.status_code == 409

        # Transfer: admins only, to a person who is a member here, audited.
        with pytest.raises(HTTPException) as refused:
            await bot_runtime.transfer_bot(
                ctx=member, bot_id=bot_id, sponsor_user_id=world.member, request=None
            )
        assert refused.value.status_code == 403
        with pytest.raises(HTTPException) as refused:
            await bot_runtime.transfer_bot(
                ctx=owner, bot_id=bot_id, sponsor_user_id=world.viewer, request=None
            )
        assert refused.value.status_code == 400
        with pytest.raises(HTTPException):
            await bot_runtime.transfer_bot(
                ctx=owner, bot_id=bot_id, sponsor_user_id=bot_id, request=None
            )
        transferred = await bot_runtime.transfer_bot(
            ctx=owner, bot_id=bot_id, sponsor_user_id=world.member, request=None
        )
        assert transferred["status"] == "active"
        [transfer] = await _audit(client, "bot.transfer")
        assert transfer["user_id"] == str(world.owner)
        assert transfer["details"]["from_sponsor_user_id"] == str(world.sponsor)
        assert transfer["details"]["to_sponsor_user_id"] == str(world.member)

        # The new sponsor holds nothing on private P1, so neither does the bot;
        # org-visible P2 still reads through both.
        bot_ctx = await _bot_ctx(raw)
        assert bot_ctx.sponsor_org_role is OrganizationRole.MEMBER
        assert await project_runtime.list_accessible_project_graph_ids(bot_ctx) == {world.p2}

        # Revoking a grant and deleting the bot.
        await bot_runtime.revoke_bot_project(
            ctx=owner, bot_id=bot_id, project_ref=world.p1, request=None
        )
        with pytest.raises(HTTPException) as refused:
            await bot_runtime.revoke_bot_project(
                ctx=owner, bot_id=bot_id, project_ref=world.p1, request=None
            )
        assert refused.value.status_code == 404
        member = await _ctx(world, world.member)
        deleted = await bot_runtime.delete_bot(ctx=member, bot_id=bot_id, request=None)
        assert deleted["api_keys_revoked"] == 1
        assert await key_runtime.authenticate_api_key(raw) is None
        assert await bot_runtime.list_bots(ctx=owner) == []
        [removed] = await _audit(client, "bot.delete")
        assert removed["user_id"] == str(world.member)
        memberships = normalize_records(
            await client.execute_query(
                "SELECT * FROM organization_members WHERE user_id = $bot;", bot=str(bot_id)
            )
        )
        assert memberships == []


@pytest.mark.asyncio
async def test_live_a_bot_cannot_climb_through_any_role_route() -> None:
    async with _live_auth_store() as client:
        world = await _seed(client)
        sponsor = await _ctx(world, world.sponsor)
        bot = await bot_runtime.create_bot(ctx=sponsor, name="oncall", request=None)
        bot_id = UUID(str(bot["id"]))
        owner_user = SimpleNamespace(id=world.owner)

        for role in (OrganizationRole.ADMIN, OrganizationRole.OWNER):
            with pytest.raises(HTTPException) as refused:
                await organization_runtime.add_org_member(
                    slug=world.slug,
                    actor_id=world.owner,
                    target_user_id=bot_id,
                    role=role,
                    request=None,
                )
            assert refused.value.detail["error"] == "bot_role_ceiling"
            with pytest.raises(HTTPException) as refused:
                await organization_runtime.update_org_member_role(
                    slug=world.slug,
                    actor_id=world.owner,
                    target_user_id=bot_id,
                    role=role,
                    request=None,
                )
            assert refused.value.detail["error"] == "bot_role_ceiling"
        for role in (ProjectRole.MAINTAINER, ProjectRole.OWNER):
            with pytest.raises(HTTPException) as refused:
                await organization_runtime.add_project_member(
                    request=None,
                    project_id=world.p2,
                    actor=owner_user,
                    org_id=world.org_id,
                    target_user_id=bot_id,
                    role=role,
                )
            assert refused.value.detail["error"] == "bot_role_ceiling"
        # A project owner may still add the bot at contributor, under the cap.
        await organization_runtime.add_project_member(
            request=None,
            project_id=world.p2,
            actor=owner_user,
            org_id=world.org_id,
            target_user_id=bot_id,
            role=ProjectRole.CONTRIBUTOR,
        )
        with pytest.raises(HTTPException) as refused:
            await organization_runtime.update_project_member_role(
                request=None,
                project_id=world.p2,
                actor=owner_user,
                org_id=world.org_id,
                target_user_id=bot_id,
                role=ProjectRole.MAINTAINER,
            )
        assert refused.value.detail["error"] == "bot_role_ceiling"

        team = await project_runtime.create_team_record(
            organization_id=world.org_id, name="Ops", created_by_user_id=world.owner
        )
        with pytest.raises(HTTPException) as refused:
            await project_runtime.add_team_member_record(
                organization_id=world.org_id, team_ref=str(team.id), user_id=bot_id
            )
        assert refused.value.detail["error"] == "bot_team_membership"

        # Another organization cannot take the bot in at all.
        other_org = uuid4()
        await _create(
            client,
            "organizations",
            {"uuid": str(other_org), "name": "Other", "slug": f"other-{other_org.hex[:10]}"},
        )
        await _create(
            client,
            "organization_members",
            {
                "uuid": str(uuid4()),
                "organization_id": str(other_org),
                "user_id": str(world.owner),
                "role": "owner",
            },
        )
        with pytest.raises(HTTPException) as refused:
            await organization_runtime.add_org_member(
                slug=f"other-{other_org.hex[:10]}",
                actor_id=world.owner,
                target_user_id=bot_id,
                role=OrganizationRole.MEMBER,
                request=None,
            )
        assert refused.value.detail["error"] == "bot_single_organization"

        # No session path mints a bot a session, and no profile edit gives it
        # an email or a password to try.
        sessions = auth_common.SurrealSessionRepository.from_client(client)
        with pytest.raises(HTTPException) as refused:
            await sessions.create_session(
                user_id=bot_id, token="t", expires_at=utcnow(), organization_id=world.org_id
            )
        assert refused.value.detail["error"] == "bot_cannot_sign_in"
        with pytest.raises(HTTPException) as refused:
            await user_runtime.update_auth_user(
                user_id=bot_id,
                email=None,
                name=None,
                avatar_url=None,
                current_password=None,
                new_password="correct horse battery staple",
                organization_id=world.org_id,
                request=None,
            )
        assert refused.value.detail["error"] == "bot_credentials_forbidden"
        with pytest.raises(HTTPException) as refused:
            await user_runtime.patch_auth_user(
                user_id=bot_id,
                updates={"email": "bot@example.test"},
                organization_id=world.org_id,
                request=None,
            )
        assert refused.value.detail["error"] == "bot_credentials_forbidden"
        renamed = await user_runtime.patch_auth_user(
            user_id=bot_id, updates={"name": "pager"}, organization_id=world.org_id, request=None
        )
        assert renamed.kind is PrincipalKind.SERVICE

        # Even a bot row forced to hold an email and password never signs in.
        from sibyl.auth.passwords import hash_password

        state = hash_password("hunter2-hunter2")
        await client.execute_query(
            "UPDATE users SET email = 'bot@example.test', password_salt = $salt, "
            "password_hash = $hash, password_iterations = $iterations WHERE uuid = $uuid;",
            salt=state.salt_hex,
            hash=state.hash_hex,
            iterations=state.iterations,
            uuid=str(bot_id),
        )
        assert (
            await user_runtime.authenticate_local_user(
                email="bot@example.test", password="hunter2-hunter2"
            )
            is None
        )
        users = surreal_auth.SurrealUserRepository.from_client(client)
        assert (
            await users.authenticate_local(email="bot@example.test", password="hunter2-hunter2")
            is None
        )

        # A bot row forced to admin still resolves as a member.
        await client.execute_query(
            "UPDATE organization_members SET role = 'admin' WHERE user_id = $uuid;",
            uuid=str(bot_id),
        )
        raw = await _issue_key(sponsor, bot_id)
        bot_ctx = await _bot_ctx(raw)
        assert bot_ctx.org_role is OrganizationRole.MEMBER


@pytest.mark.asyncio
async def test_live_the_team_listing_and_audit_log_mark_bots() -> None:
    async with _live_auth_store() as client:
        world = await _seed(client)
        sponsor = await _ctx(world, world.sponsor)
        bot = await bot_runtime.create_bot(ctx=sponsor, name="oncall", request=None)
        bot_id = UUID(str(bot["id"]))
        await audit_runtime.log_audit_event(
            action="memory.capture",
            user_id=bot_id,
            organization_id=world.org_id,
            request=None,
            details={},
        )

        rows = await organization_runtime.list_org_members(slug=world.slug, actor_id=world.owner)
        kinds = {row["user"]["id"]: row["user"]["kind"] for row in rows}
        assert kinds[str(bot_id)] == "service"
        assert kinds[str(world.sponsor)] == "user"
        [bot_row] = [row for row in rows if row["user"]["id"] == str(bot_id)]
        assert bot_row["user"]["sponsor_user_id"] == str(world.sponsor)

        events, total = await audit_runtime.list_audit_events(organization_id=world.org_id)
        assert total == 2
        by_action = {event["action"]: event for event in events}
        assert by_action["memory.capture"]["actor_kind"] == "service"
        assert by_action["bot.create"]["actor_kind"] == "user"
        resource_events, _ = await audit_runtime.list_audit_events(
            organization_id=world.org_id, resource=str(bot_id)
        )
        assert all(event.get("actor_kind") for event in resource_events)
