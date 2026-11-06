"""Assigning, retiring and looking up organization member handles.

Every path that creates a membership goes through ``create_membership``,
which picks the member's handle and writes the membership under one lock per
organization, so two members joining at once can never take the same handle.
The lock is the coordination backend's entity lock, shared across replicas
under Redis coordination. Healing takes the same lock.

A handle stays with its member. When they leave it is retired rather than
freed (``retire_member_handles``, before the membership is deleted), so the
next person to join can't inherit their inbox's history and the tasks still
assigned to it; the same person rejoining takes it back.

A membership can still end up without a handle of its own: written by a
replica from before handles during a rolling deploy, restored from an old
archive, or merged in from another team holding the same handle. Startup
heals every organization once, an archive restore heals what it loaded, and
the lookups heal on the spot: when what they read shows a missing,
duplicate or reserved handle, they repair the organization under the lock
before answering, so nobody stays unreachable or shares an inbox.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Protocol
from uuid import uuid4

from sibyl.locks import entity_lock
from sibyl_core.auth.handles import RESERVED_HANDLES, handle_base, pick_handle
from sibyl_core.backends.surreal.member_handles import backfill_member_handles
from sibyl_core.backends.surreal.records import normalize_records

HANDLES_LOCK_ENTITY = "member-handles"


class _Queries(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


@asynccontextmanager
async def handles_locked(organization_id: str) -> AsyncGenerator[None]:
    """Hold the organization's handle lock while a handle is chosen and written."""
    async with entity_lock(organization_id, HANDLES_LOCK_ENTITY, blocking=True) as token:
        if not token:
            msg = f"could not lock member handles for organization {organization_id}"
            raise RuntimeError(msg)
        yield


async def _rows(client: _Queries, query: str, **params: object) -> list[dict[str, object]]:
    return normalize_records(await client.execute_query(query, **params))


async def _identity(client: _Queries, user_id: str) -> tuple[str | None, str | None]:
    rows = await _rows(
        client, "SELECT email, name FROM users WHERE uuid = $uuid LIMIT 1;", uuid=user_id
    )
    user = rows[0] if rows else {}
    email, name = user.get("email"), user.get("name")
    return (str(email) if email else None, str(name) if name else None)


async def _members(client: _Queries, organization_id: str) -> list[dict[str, object]]:
    return await _rows(
        client,
        "SELECT user_id, handle FROM organization_members WHERE organization_id = $organization_id;",
        organization_id=organization_id,
    )


async def _retired(client: _Queries, organization_id: str) -> list[dict[str, object]]:
    return await _rows(
        client,
        "SELECT user_id, handle FROM retired_member_handles "
        "WHERE organization_id = $organization_id;",
        organization_id=organization_id,
    )


def _needs_healing(handles: Iterable[object]) -> bool:
    """Whether any member lacks a handle, holds a reserved one, or shares one."""
    counts = Counter(str(handle or "") for handle in handles)
    return bool(
        counts[""] or any(counts[handle] > 1 or handle in RESERVED_HANDLES for handle in counts)
    )


async def heal_member_handles(client: _Queries, *, organization_id: str) -> int:
    """Give every member of the organization a handle of their own, under the lock."""
    async with handles_locked(organization_id):
        return await backfill_member_handles(client.execute_query, organization_id=organization_id)


async def heal_all_member_handles(client: _Queries) -> int:
    """Heal every organization whose members' handles need it, each under its own lock."""
    rows = await _rows(client, "SELECT organization_id, handle FROM organization_members;")
    by_org: dict[str, list[object]] = {}
    for row in rows:
        by_org.setdefault(str(row.get("organization_id") or ""), []).append(row.get("handle"))
    healed = 0
    for organization_id, handles in sorted(by_org.items()):
        if organization_id and _needs_healing(handles):
            healed += await heal_member_handles(client, organization_id=organization_id)
    return healed


async def first_member_handle(client: _Queries, user_id: str) -> str:
    """The handle of a new organization's first member: nothing is taken, but reserved still is."""
    email, name = await _identity(client, user_id)
    return pick_handle(handle_base(email, name), ())


async def create_membership(client: _Queries, record: Mapping[str, object]) -> object:
    """Create a membership carrying the member's handle; returns the CREATE's result.

    A member rejoining takes back the handle they held when they left; anyone
    else gets a fresh one, past every handle held or retired in the
    organization.
    """
    organization_id = str(record["organization_id"])
    user_id = str(record["user_id"])
    async with handles_locked(organization_id):
        retired = await _retired(client, organization_id)
        held = await _members(client, organization_id)
        own = next((str(row["handle"]) for row in retired if row.get("user_id") == user_id), None)
        if own is not None and own not in {str(row.get("handle")) for row in held}:
            await client.execute_query(
                "DELETE FROM retired_member_handles "
                "WHERE organization_id = $organization_id AND handle = $handle;",
                organization_id=organization_id,
                handle=own,
            )
            handle = own
        else:
            email, name = await _identity(client, user_id)
            taken = {str(row["handle"]) for row in (*held, *retired) if row.get("handle")}
            handle = pick_handle(handle_base(email, name), taken)
        return await client.execute_query(
            "CREATE organization_members CONTENT $record;", record={**record, "handle": handle}
        )


async def retire_member_handles(
    client: _Queries, *, user_id: str, organization_id: str | None = None
) -> None:
    """Retire the handles a member holds, in one organization or all, before they leave."""
    query = "SELECT organization_id, handle FROM organization_members WHERE user_id = $user_id"
    if organization_id is not None:
        query += " AND organization_id = $organization_id"
    for row in await _rows(client, f"{query};", user_id=user_id, organization_id=organization_id):
        if not row.get("handle"):
            continue
        await client.execute_query(
            "CREATE retired_member_handles CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(row.get("organization_id")),
                "handle": str(row["handle"]),
                "user_id": user_id,
            },
        )


async def member_handle(client: _Queries, *, organization_id: str, user_id: str) -> str | None:
    """The handle a member holds in an organization, or None when they are not a member."""
    query = (
        "SELECT handle FROM organization_members "
        "WHERE organization_id = $organization_id AND user_id = $user_id LIMIT 1;"
    )
    rows = await _rows(client, query, organization_id=organization_id, user_id=user_id)
    if not rows:
        return None
    handle = str(rows[0].get("handle") or "")
    if (
        not handle
        or handle in RESERVED_HANDLES
        or len(await _holders(client, organization_id, handle)) > 1
    ):
        await heal_member_handles(client, organization_id=organization_id)
        rows = await _rows(client, query, organization_id=organization_id, user_id=user_id)
        handle = str(rows[0].get("handle") or "") if rows else ""
    return handle or None


async def _holders(client: _Queries, organization_id: str, handle: str) -> list[str]:
    rows = await _rows(
        client,
        "SELECT user_id FROM organization_members "
        "WHERE organization_id = $organization_id AND handle = $handle LIMIT 2;",
        organization_id=organization_id,
        handle=handle,
    )
    return [str(row.get("user_id") or "") for row in rows]


async def organization_handles(client: _Queries, *, organization_id: str) -> dict[str, str]:
    """Every member's handle in the organization, with the user holding it."""
    members = await _members(client, organization_id)
    if _needs_healing(row.get("handle") for row in members):
        await heal_member_handles(client, organization_id=organization_id)
        members = await _members(client, organization_id)
    return {
        str(row["handle"]): str(row.get("user_id") or "") for row in members if row.get("handle")
    }


async def handle_owner(client: _Queries, *, organization_id: str, handle: str) -> str | None:
    """The user holding ``handle`` in the organization, after healing what the read shows."""
    holders = await _holders(client, organization_id, handle)
    if len(holders) == 1:
        return holders[0]
    # Two holders share an inbox, or a member without a handle yet may be
    # the one asked for: heal, then answer from the healed handles.
    return (await organization_handles(client, organization_id=organization_id)).get(handle)


async def handle_retired(client: _Queries, *, organization_id: str, handle: str) -> bool:
    """Whether ``handle`` belonged to a member who has left the organization."""
    rows = await _rows(
        client,
        "SELECT handle FROM retired_member_handles "
        "WHERE organization_id = $organization_id AND handle = $handle LIMIT 1;",
        organization_id=organization_id,
        handle=handle,
    )
    return bool(rows)


__all__ = [
    "HANDLES_LOCK_ENTITY",
    "create_membership",
    "first_member_handle",
    "handle_owner",
    "handle_retired",
    "handles_locked",
    "heal_all_member_handles",
    "heal_member_handles",
    "member_handle",
    "organization_handles",
    "retire_member_handles",
]
