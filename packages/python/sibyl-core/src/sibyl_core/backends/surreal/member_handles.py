"""Give every organization member a handle of their own, in the order they joined.

Memberships can lack a handle (written before handles existed, or restored
from an old archive) or share one (two teams merged into one organization,
or an old writer racing a new one). A member without a handle, or holding a
reserved one, gets a fresh handle; of two holding the same handle, the
earlier to join keeps it and the later gets a new one. Handles retired when
a member left count as taken, so nobody inherits an inbox. Nothing else
changes, so it is safe to run again.

Callers run this under the organization's handle lock (see the API's
``member_handles``), or a member joining meanwhile could take the same
handle. Each membership is updated by its own statement on the uuid index:
no loop runs inside the database.
"""

from __future__ import annotations

from datetime import datetime

from sibyl_core.auth.handles import RESERVED_HANDLES, handle_base, pick_handle
from sibyl_core.backends.surreal.records import coerce_datetime, normalize_records
from sibyl_core.backends.surreal.schema_version import SurrealExecute

_MEMBERS = "SELECT uuid, organization_id, user_id, handle, created_at FROM organization_members"
_RETIRED = "SELECT organization_id, handle FROM retired_member_handles"
_SET_HANDLE = "UPDATE organization_members SET handle = $handle WHERE uuid = $uuid RETURN NONE;"


def _joined(row: dict[str, object]) -> tuple[str, datetime, str]:
    try:
        joined = coerce_datetime(row.get("created_at")) or datetime.min
    except ValueError:
        joined = datetime.min
    return (str(row.get("organization_id") or ""), joined, str(row.get("uuid") or ""))


async def backfill_member_handles(
    execute_query: SurrealExecute, *, organization_id: str | None = None
) -> int:
    """Assign a handle to every membership lacking one of its own; returns how many changed."""
    scope = "" if organization_id is None else " WHERE organization_id = $organization_id"
    members = normalize_records(
        await execute_query(f"{_MEMBERS}{scope};", organization_id=organization_id)
    )
    retired = normalize_records(
        await execute_query(f"{_RETIRED}{scope};", organization_id=organization_id)
    )
    taken: dict[str, set[str]] = {}
    missing: list[dict[str, object]] = []
    for row in sorted(members, key=_joined):
        org_taken = taken.setdefault(str(row.get("organization_id")), set())
        handle = str(row.get("handle") or "")
        if handle and handle not in org_taken and handle not in RESERVED_HANDLES:
            org_taken.add(handle)
        else:
            missing.append(row)
    # A member who holds a handle keeps it; a retired one only keeps anyone
    # else from being given it.
    for row in retired:
        taken.setdefault(str(row.get("organization_id")), set()).add(str(row.get("handle")))
    if not missing:
        return 0
    user_ids = sorted({str(row.get("user_id") or "") for row in missing})
    users = {
        str(user.get("uuid")): user
        for user in normalize_records(
            await execute_query(
                "SELECT uuid, email, name FROM users WHERE uuid IN $user_ids;", user_ids=user_ids
            )
        )
    }
    for row in missing:
        user = users.get(str(row.get("user_id") or ""), {})
        email = user.get("email")
        name = user.get("name")
        org_taken = taken.setdefault(str(row.get("organization_id")), set())
        handle = pick_handle(
            handle_base(str(email) if email else None, str(name) if name else None), org_taken
        )
        org_taken.add(handle)
        await execute_query(_SET_HANDLE, handle=handle, uuid=str(row.get("uuid")))
    return len(missing)


__all__ = ["backfill_member_handles"]
