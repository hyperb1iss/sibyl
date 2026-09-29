"""Operator retirement reads persisted authority and defaults to a preview."""

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from sibyl.cli import migrate as migrate_cli
from sibyl.persistence import shared_scope_retirement as authority_runtime
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema
from sibyl_core.migrate.shared_scope_retirement import SharedScopeRetirementReceipt


@pytest.mark.asyncio
async def test_shared_retirement_authority_uses_only_same_org_persisted_grants(monkeypatch):
    client = SurrealAuthClient(
        url=os.environ.get("SIBYL_SHARED_RETIREMENT_TEST_URL", "memory://"),
        username="root",
        password="root",
        namespace=f"retirement_auth_{uuid4().hex}",
    )
    await bootstrap_auth_schema(client)
    org_id, foreign_org, team_id, foreign_team, member, admin, outsider = [
        str(uuid4()) for _ in range(7)
    ]
    records = {
        "users": [
            {"uuid": principal, "email": f"{principal}@example.test"}
            for principal in (member, admin, outsider)
        ],
        "organizations": [
            {"uuid": org_id, "name": "Selected", "slug": org_id},
            {"uuid": foreign_org, "name": "Foreign", "slug": foreign_org},
        ],
        "teams": [
            {"uuid": team_id, "organization_id": org_id, "name": "Same org", "slug": team_id},
            {
                "uuid": foreign_team,
                "organization_id": foreign_org,
                "name": "Foreign",
                "slug": foreign_team,
            },
        ],
        "organization_members": [
            {"uuid": str(uuid4()), "organization_id": org_id, "user_id": member, "role": "member"},
            {"uuid": str(uuid4()), "organization_id": org_id, "user_id": admin, "role": "admin"},
            {
                "uuid": str(uuid4()),
                "organization_id": foreign_org,
                "user_id": outsider,
                "role": "owner",
            },
        ],
        "team_members": [
            {"uuid": str(uuid4()), "team_id": team_id, "user_id": member, "role": "member"},
            {"uuid": str(uuid4()), "team_id": foreign_team, "user_id": outsider, "role": "admin"},
        ],
    }
    for table, rows in records.items():
        for record in rows:
            await client.execute_query(f"CREATE {table} CONTENT $record;", record=record)

    @asynccontextmanager
    async def auth_client():
        yield client

    monkeypatch.setattr(authority_runtime, "surreal_auth_client_scope", auth_client)
    try:
        authority = await authority_runtime.resolve_shared_scope_retirement_authority(org_id)
        assert authority.organization_id == org_id
        assert authority.team_ids == frozenset({team_id})
        assert authority.organization_members == frozenset({member, admin})
        assert authority.organization_admins == frozenset({admin})
        assert authority.team_memberships == frozenset({(team_id, member)})
        await client.execute_query(
            "UPDATE users SET deleted_at=time::now() WHERE uuid=$uuid;",
            uuid=admin,
        )
        retired_authority = await authority_runtime.resolve_shared_scope_retirement_authority(
            org_id
        )
        assert admin not in retired_authority.organization_members
        assert retired_authority.organization_admins == frozenset()
        with pytest.raises(ValueError, match="not present"):
            await authority_runtime.resolve_shared_scope_retirement_authority(str(uuid4()))
    finally:
        await client.close()


def test_retire_shared_operator_defaults_to_dry_run_and_requires_org(monkeypatch):
    from sibyl_core.migrate import shared_scope_retirement as migration

    org_id = str(uuid4())
    calls = []

    async def retire(**kwargs):
        calls.append(kwargs)
        return SharedScopeRetirementReceipt(organization_id=org_id, dry_run=kwargs["dry_run"])

    monkeypatch.setattr(migration, "retire_shared_captures", retire)
    runner = CliRunner()
    result = runner.invoke(migrate_cli.app, ["retire-shared", "--org-id", org_id])
    assert result.exit_code == 0, result.output
    assert calls[0]["organization_id"] == org_id
    assert calls[0]["dry_run"] is True
    assert '"success": true' in result.output
    result = runner.invoke(migrate_cli.app, ["retire-shared", "--org-id", org_id, "--apply"])
    assert result.exit_code == 0, result.output
    assert calls[1]["dry_run"] is False
    assert runner.invoke(migrate_cli.app, ["retire-shared"]).exit_code != 0
    assert runner.invoke(migrate_cli.app, ["retire-shared", "--org-id", "guess"]).exit_code != 0
    assert len(calls) == 2
