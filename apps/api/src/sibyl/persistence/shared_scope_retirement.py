"""Resolve the persisted authority needed to retire shared captures."""

from sibyl.persistence.surreal.auth import surreal_auth_client_scope
from sibyl_core.backends.surreal.records import normalize_records, raise_on_error
from sibyl_core.migrate.shared_scope_retirement import SharedScopeAuthority


async def resolve_shared_scope_retirement_authority(organization_id: str) -> SharedScopeAuthority:
    async with surreal_auth_client_scope() as client:
        result = await client.execute_query(
            """
            RETURN {
                organizations: (SELECT VALUE uuid FROM organizations
                    WHERE uuid = $organization_id LIMIT 1),
                teams: (SELECT uuid FROM teams WHERE organization_id = $organization_id),
                principals: (SELECT user_id, role FROM organization_members
                    WHERE organization_id = $organization_id
                        AND user_id IN (SELECT VALUE uuid FROM users WHERE deleted_at = NONE)),
                memberships: (SELECT team_id, user_id FROM team_members
                    WHERE team_id IN (SELECT VALUE uuid FROM teams
                        WHERE organization_id = $organization_id)),
            };
            """,
            organization_id=organization_id,
        )
    raise_on_error(result, query="shared_scope_retirement_authority")
    payloads = normalize_records(result)
    organizations = payloads[0].get("organizations") if payloads else None
    if not isinstance(organizations, list) or organization_id not in organizations:
        raise ValueError("migration organization is not present in persisted authority")
    payload = payloads[0]
    principals = normalize_records(payload.get("principals"))
    return SharedScopeAuthority(
        organization_id=organization_id,
        team_ids=frozenset(str(team["uuid"]) for team in normalize_records(payload.get("teams"))),
        organization_members=frozenset(str(principal["user_id"]) for principal in principals),
        organization_admins=frozenset(
            str(principal["user_id"])
            for principal in principals
            if principal.get("role") in {"owner", "admin"}
        ),
        team_memberships=frozenset(
            (str(member["team_id"]), str(member["user_id"]))
            for member in normalize_records(payload.get("memberships"))
        ),
    )
