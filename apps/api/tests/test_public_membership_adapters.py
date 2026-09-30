"""Public adapters consume persisted authority, including retired legacy rows.

The REST dependency decodes signed fixture identity without a session table.
MCP reads the same signed token from a fixture access-token provider. Native
membership resolution, all retrieval lanes, registration, serialization, and
CLI HTTP transport remain real. Each matrix row executes one public read.
"""

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from mcp.server import MCPServer
from pydantic import SecretStr

from sibyl.api.routes import context as context_routes
from sibyl.auth.dependencies import _api_key_claims
from sibyl.auth.jwt import create_access_token, verify_access_token
from sibyl.mcp_tools import context as mcp_context, retrieval as mcp_retrieval
from sibyl.persistence.auth_runtime import authenticate_api_key
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver
from sibyl.persistence.surreal.auth_runtime import _common as auth_common
from sibyl.persistence.surreal.auth_runtime.api_keys import create_api_key_for_user
from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
)
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.config import settings
from sibyl_core.migrate.shared_scope_retirement import SharedScopeAuthority, retire_shared_captures
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services import content_client, content_models, graph_client
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import save_raw_memory
from sibyl_core.services.graph import SurrealGraphClient
from sibyl_core.tools import context as core_context, search as core_search, usage_exposure

SCOPES = (
    "private",
    "project",
    "team",
    "delegated",
    "mapped_shared",
    "tombstoned_shared",
    "unprocessed_shared",
    "organization",
    "public",
)
ROLES = ("owner", "admin", "member", "viewer")
CLI_EXECUTABLE = str(Path(sys.executable).with_name("sibyl"))


def pack_ids(payload):
    return {
        item["id"].removeprefix("raw_memory:")
        for section in payload["sections"]
        for item in section["items"]
    }


async def seed_public_memberships(
    auth, content, *, org, owner, peer, team, delegation, project, other_project
):
    records = {
        "users": [{"uuid": user, "email": f"{user}@example.test"} for user in (owner, peer)],
        "organizations": [{"uuid": org, "name": "Proof", "slug": org}],
        "organization_members": [
            {"uuid": str(uuid4()), "organization_id": org, "user_id": user, "role": "member"}
            for user in (owner, peer)
        ],
        "teams": [{"uuid": team, "organization_id": org, "name": "Proof", "slug": team}],
        "team_members": [
            {"uuid": str(uuid4()), "team_id": team, "user_id": owner, "role": "member"}
        ],
        "projects": [
            {
                "uuid": proj,
                "organization_id": org,
                "name": proj,
                "slug": proj,
                "graph_project_id": proj,
                "visibility": "private",
            }
            for proj in (project, other_project)
        ],
        "project_members": [
            {
                "uuid": str(uuid4()),
                "organization_id": org,
                "project_id": member_project,
                "user_id": owner,
                "role": "project_viewer",
            }
            for member_project in (project, other_project)
        ],
        "memory_spaces": [
            {
                "uuid": delegation,
                "organization_id": org,
                "memory_scope": "delegated",
                "scope_key": delegation,
                "created_by_user_id": owner,
            }
        ],
        "memory_space_members": [
            {
                "uuid": str(uuid4()),
                "organization_id": org,
                "space_id": delegation,
                "principal_type": "user",
                "principal_id": owner,
                "created_by_user_id": owner,
            }
        ],
    }
    for table, rows in records.items():
        for record in rows:
            await auth.execute_query(f"CREATE {table} CONTENT $record;", record=record)
    memories = {}
    for case in SCOPES:
        scope = MemoryScope.SHARED if case.endswith("shared") else MemoryScope(case)
        key = (
            project
            if scope is MemoryScope.PROJECT
            else team
            if scope in {MemoryScope.TEAM, MemoryScope.SHARED}
            else delegation
            if scope is MemoryScope.DELEGATED
            else None
        )
        if case == "tombstoned_shared":
            key = str(uuid4())
        memory = RawMemory(
            id=str(uuid4()),
            organization_id=org,
            principal_id=owner,
            source_id=f"fixture:{case}",
            memory_scope=scope,
            scope_key=key,
            project_id=project if scope is MemoryScope.PROJECT else None,
            raw_content=f"scopeproof{case.replace('_', '')} retained verbatim evidence",
            captured_at=content_models.utcnow(),
            created_at=content_models.utcnow(),
        )
        if scope is MemoryScope.SHARED:
            await content.execute_query(
                "CREATE raw_captures CONTENT $record;",
                record=content_models.raw_memory_record(memory),
            )
        else:
            memory = await save_raw_memory(memory, embedding_provider=None)
        memories[case] = memory

    # Leave one legacy row beyond this migration snapshot. Its ID is retained.
    unprocessed = memories.pop("unprocessed_shared")
    await content.execute_query("DELETE raw_captures WHERE uuid=$uuid;", uuid=unprocessed.id)
    authority = SharedScopeAuthority(
        organization_id=org,
        team_ids=frozenset({team}),
        organization_members=frozenset({owner, peer}),
        organization_admins=frozenset(),
        team_memberships=frozenset({(team, owner)}),
    )

    async def authority_provider():
        return authority

    receipt = await retire_shared_captures(
        organization_id=org, authority_provider=authority_provider, dry_run=False
    )
    assert receipt.success
    await content.execute_query(
        "CREATE raw_captures CONTENT $record;", record=content_models.raw_memory_record(unprocessed)
    )
    memories["unprocessed_shared"] = unprocessed

    return memories


@pytest.fixture
async def public_membership_runtime(monkeypatch, tmp_path):
    url = os.environ.get("SIBYL_SHARED_RETIREMENT_TEST_URL", "memory://")
    namespace = f"public_membership_{uuid4().hex}"
    auth = SurrealAuthClient(url=url, username="root", password="root", namespace=namespace)
    content = SurrealContentClient(url=url, username="root", password="root", namespace=namespace)
    monkeypatch.setattr(settings, "surreal_url", url)
    await bootstrap_auth_schema(auth)
    await bootstrap_content_schema(content)
    org, owner, peer, team, delegation, project, other_project = [str(uuid4()) for _ in range(7)]

    @asynccontextmanager
    async def auth_scope():
        yield auth

    async def content_scope():
        return content

    monkeypatch.setattr(auth_common, "surreal_auth_client_scope", auth_scope)
    monkeypatch.setattr(content_client, "get_shared_surreal_content_client", content_scope)
    monkeypatch.setattr(usage_exposure, "get_shared_surreal_content_client", content_scope)
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("isolated-public-membership-proof-" * 3))
    monkeypatch.setattr(core_context, "configured_embedding_provider", lambda: None)
    monkeypatch.setattr(core_search, "configured_embedding_provider", lambda: None)
    native_graph = SurrealGraphClient(
        group_id=org, url=url, username="root", password="root", namespace_prefix=f"{namespace}_"
    )
    monkeypatch.setattr(graph_client, "_new_graph_client", lambda group: native_graph)
    resolver = SurrealAuthContextResolver.from_client(auth)
    memories = await seed_public_memberships(
        auth,
        content,
        org=org,
        owner=owner,
        peer=peer,
        team=team,
        delegation=delegation,
        project=project,
        other_project=other_project,
    )

    app = FastAPI()

    @app.middleware("http")
    async def fixture_identity(request: Request, call_next):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if token.startswith("sk_"):
            key = await authenticate_api_key(token)
            assert key is not None
            claims = _api_key_claims(key, scopes=list(key.scopes or []))
        else:
            claims = verify_access_token(token)
        request.state.auth_context = await resolver.resolve(claims)
        return await call_next(request)

    app.include_router(context_routes.router, prefix="/api")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if server_task.done():
            await server_task
        await asyncio.sleep(0.01)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    mcp = MCPServer("persisted-membership-proof")
    mcp_retrieval.register_retrieval_tools(mcp)
    active_token = {"value": ""}
    monkeypatch.setattr(
        mcp_context, "get_access_token", lambda: SimpleNamespace(token=active_token["value"])
    )
    runtime = SimpleNamespace(
        app=app,
        auth=auth,
        content=content,
        resolver=resolver,
        org=org,
        owner=owner,
        peer=peer,
        team=team,
        delegation=delegation,
        project=project,
        other_project=other_project,
        memories=memories,
        base=base,
        mcp=mcp,
        active_token=active_token,
        tmp_path=tmp_path,
    )
    try:
        yield runtime
    finally:
        server.should_exit = True
        await server_task
        sock.close()
        graph_client._clients.pop(org, None)
        graph_client.mark_graph_schema_dirty(org)
        await native_graph.close()
        await content.close()
        await auth.close()


async def public_read(runtime, adapter, token, goal, *, project=None):
    if adapter == "rest":
        async with httpx.AsyncClient(base_url=runtime.base) as client:
            response = await client.post(
                "/api/context/pack",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "goal": goal,
                    "project": project,
                    "include_related": False,
                    "record_exposure": False,
                },
            )
        assert response.status_code == 200, response.text
        return response.json()
    if adapter == "mcp":
        runtime.active_token["value"] = token
        result = await runtime.mcp.call_tool(
            "context",
            {
                "goal": goal,
                "project": project,
                "all_projects": project is None,
                "include_related": False,
            },
        )
        assert not result.is_error, result
        return result.structured_content
    env = {
        **os.environ,
        "SIBYL_API_URL": f"{runtime.base}/api",
        "SIBYL_AUTH_TOKEN": token,
        "XDG_CONFIG_HOME": str(runtime.tmp_path / "config"),
        "XDG_DATA_HOME": str(runtime.tmp_path / "data"),
    }
    args = ["--project", project] if project else ["--all"]
    process = await asyncio.create_subprocess_exec(
        CLI_EXECUTABLE,
        "context",
        goal,
        *args,
        "--json",
        "--no-related",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode() + stdout.decode()
    return json.loads(stdout)


@pytest.mark.asyncio
async def test_registered_public_read_authority_216_controls(public_membership_runtime):
    runtime = public_membership_runtime
    ledger = []
    for role in ROLES:
        await runtime.auth.execute_query(
            "UPDATE organization_members SET role=$role WHERE organization_id=$org;",
            role=role,
            org=runtime.org,
        )
        for principal in (runtime.owner, runtime.peer):
            token = create_access_token(
                user_id=UUID(principal),
                organization_id=UUID(runtime.org),
                extra_claims={"role": "owner", "accessible_teams": [str(uuid4())]},
            )
            for case in SCOPES:
                expected = (
                    principal == runtime.owner
                    if case in {"private", "delegated"}
                    else principal == runtime.owner or role in {"owner", "admin"}
                    if case in {"project", "team", "mapped_shared"}
                    else False
                )
                for adapter in ("rest", "mcp", "cli"):
                    payload = await public_read(
                        runtime, adapter, token, f"scopeproof{case.replace('_', '')}"
                    )
                    seen = runtime.memories[case].id in pack_ids(payload)
                    ledger.append(
                        {
                            "role": role,
                            "principal": "owner" if principal == runtime.owner else "peer",
                            "scope": case,
                            "adapter": adapter,
                            "expected": expected,
                            "observed": seen,
                        }
                    )
                    assert seen is expected, ledger[-1]
    assert len(ledger) == 216
    receipt_path = os.environ.get("SIBYL_SHARED_CONTROL_LEDGER")
    if receipt_path:
        await asyncio.to_thread(
            Path(receipt_path).write_text,
            json.dumps(
                {
                    "controls": ledger,
                    "transport": {
                        "rest": "loopback HTTP",
                        "mcp": "registered MCPServer.call_tool",
                        "cli": "registered CLI subprocess over loopback REST",
                    },
                    "authority": "native persisted resolver",
                    "identity_seam": "signed fixture JWT without session validation",
                },
                indent=2,
            ),
        )


@pytest.mark.asyncio
async def test_persisted_membership_revocation_and_key_ceiling(public_membership_runtime):
    runtime = public_membership_runtime
    claims = {"sub": runtime.owner, "org": runtime.org, "accessible_teams": ["forged"]}
    ctx = await runtime.resolver.resolve(claims)
    assert ctx.accessible_teams == frozenset({runtime.team})
    assert ctx.accessible_delegations == frozenset({runtime.delegation})
    foreign_org, foreign_team = str(uuid4()), str(uuid4())
    await runtime.auth.execute_query(
        "CREATE teams CONTENT $record;",
        record={
            "uuid": foreign_team,
            "organization_id": foreign_org,
            "name": "Foreign",
            "slug": foreign_team,
        },
    )
    await runtime.auth.execute_query(
        "CREATE team_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "team_id": foreign_team,
            "user_id": runtime.owner,
            "role": "member",
        },
    )
    ctx = await runtime.resolver.resolve(claims)
    assert ctx.accessible_teams == frozenset({runtime.team})
    await runtime.auth.execute_query(
        "UPDATE memory_spaces SET state=$state WHERE uuid=$uuid;",
        uuid=runtime.delegation,
        state="disabled",
    )
    assert (await runtime.resolver.resolve(claims)).accessible_delegations == frozenset()
    await runtime.auth.execute_query(
        "UPDATE memory_spaces SET state=$state WHERE uuid=$uuid;",
        uuid=runtime.delegation,
        state="active",
    )
    token = create_access_token(
        user_id=UUID(runtime.owner),
        organization_id=UUID(runtime.org),
        extra_claims={
            "api_key_id": str(uuid4()),
            "api_key_memory_scope_keys": [
                memory_scope_policy_key(MemoryScope.PRIVATE, runtime.owner)
            ],
        },
    )
    payload = await public_read(runtime, "rest", token, "scopeproofteam")
    assert runtime.memories["team"].id not in pack_ids(payload)
    await runtime.auth.execute_query("UPDATE memory_space_members SET expires_at=time::now() - 1s;")
    await runtime.auth.execute_query("DELETE team_members;")
    ctx = await runtime.resolver.resolve(claims)
    assert ctx.accessible_teams == frozenset()
    assert ctx.accessible_delegations == frozenset()
    for case in ("team", "delegated"):
        token = create_access_token(user_id=UUID(runtime.owner), organization_id=UUID(runtime.org))
        for adapter in ("rest", "mcp", "cli"):
            payload = await public_read(runtime, adapter, token, f"scopeproof{case}")
            assert runtime.memories[case].id not in pack_ids(payload)
    await runtime.auth.execute_query(
        "DELETE organization_members WHERE user_id=$user;", user=runtime.owner
    )
    runtime.active_token["value"] = token
    assert await mcp_context.get_context() is None


@pytest.mark.asyncio
async def test_registered_api_key_scope_ceiling_and_selected_project(public_membership_runtime):
    runtime = public_membership_runtime
    spaces = {}
    for scope, key in ((MemoryScope.PRIVATE, runtime.owner), (MemoryScope.TEAM, runtime.team)):
        space = str(uuid4())
        await runtime.auth.execute_query(
            "CREATE memory_spaces CONTENT $record;",
            record={
                "uuid": space,
                "organization_id": runtime.org,
                "memory_scope": scope.value,
                "scope_key": key,
                "created_by_user_id": runtime.owner,
            },
        )
        spaces[scope] = space
    for allowed_scope in (MemoryScope.PRIVATE, MemoryScope.TEAM):
        _key, token = await create_api_key_for_user(
            organization_id=UUID(runtime.org),
            user_id=UUID(runtime.owner),
            name=f"Isolated {allowed_scope.value} ceiling",
            live=False,
            scopes=["mcp", "api:read"],
            memory_space_ids=[UUID(spaces[allowed_scope])],
            expires_at=None,
            request=None,
        )
        for case in ("private", "team", "delegated", "project"):
            for adapter in ("rest", "mcp", "cli"):
                payload = await public_read(runtime, adapter, token, f"scopeproof{case}")
                assert (runtime.memories[case].id in pack_ids(payload)) is (
                    case == allowed_scope.value
                )
    token = create_access_token(user_id=UUID(runtime.owner), organization_id=UUID(runtime.org))
    for adapter in ("rest", "mcp", "cli"):
        selected = await public_read(
            runtime, adapter, token, "scopeproofproject", project=runtime.project
        )
        excluded = await public_read(
            runtime, adapter, token, "scopeproofproject", project=runtime.other_project
        )
        assert runtime.memories["project"].id in pack_ids(selected)
        assert runtime.memories["project"].id not in pack_ids(excluded)
