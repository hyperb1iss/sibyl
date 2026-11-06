"""Assigning and looking up organization member handles.

Every path that creates a membership goes through ``create_membership``,
which picks the member's handle and writes the membership under one lock per
organization, so two members joining at once can never take the same handle.
The lock is the coordination backend's entity lock, shared across replicas
under Redis coordination.

A membership can still arrive without a handle: written by a replica that
predates handles during a rolling deploy, or restored from an old archive.
The lookups heal that on the spot: when the members they read include one
without a handle, they assign the missing handles under the same lock before
answering, so nobody stays unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Protocol

from sibyl.locks import entity_lock
from sibyl_core.auth.handles import handle_base, pick_handle
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


async def heal_member_handles(client: _Queries, *, organization_id: str) -> int:
    """Give every member of the organization still without a handle one, under the lock."""
    async with handles_locked(organization_id):
        return await backfill_member_handles(client.execute_query, organization_id=organization_id)


async def first_member_handle(client: _Queries, user_id: str) -> str:
    """The handle of a new organization's first member: nothing is taken, but reserved still is."""
    email, name = await _identity(client, user_id)
    return pick_handle(handle_base(email, name), ())


async def create_membership(client: _Queries, record: Mapping[str, object]) -> object:
    """Create a membership carrying a fresh handle; returns the CREATE's result."""
    organization_id = str(record["organization_id"])
    async with handles_locked(organization_id):
        email, name = await _identity(client, str(record["user_id"]))
        taken = {
            str(row["handle"])
            for row in await _members(client, organization_id)
            if row.get("handle")
        }
        return await client.execute_query(
            "CREATE organization_members CONTENT $record;",
            record={**record, "handle": pick_handle(handle_base(email, name), taken)},
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
    if not rows[0].get("handle"):
        await heal_member_handles(client, organization_id=organization_id)
        rows = await _rows(client, query, organization_id=organization_id, user_id=user_id)
    handle = rows[0].get("handle") if rows else None
    return str(handle) if handle else None


async def organization_handles(client: _Queries, *, organization_id: str) -> dict[str, str]:
    """Every member's handle in the organization, with the user holding it."""
    members = await _members(client, organization_id)
    if any(not row.get("handle") for row in members):
        await heal_member_handles(client, organization_id=organization_id)
        members = await _members(client, organization_id)
    return {
        str(row["handle"]): str(row.get("user_id") or "") for row in members if row.get("handle")
    }


async def handle_owner(client: _Queries, *, organization_id: str, handle: str) -> str | None:
    """The user holding ``handle`` in the organization, healing missing handles before a miss."""
    query = (
        "SELECT user_id FROM organization_members "
        "WHERE organization_id = $organization_id AND handle = $handle LIMIT 1;"
    )
    rows = await _rows(client, query, organization_id=organization_id, handle=handle)
    if not rows:
        # A member without a handle yet may be the one asked for.
        return (await organization_handles(client, organization_id=organization_id)).get(handle)
    owner = rows[0].get("user_id")
    return str(owner) if owner else None


__all__ = [
    "HANDLES_LOCK_ENTITY",
    "create_membership",
    "first_member_handle",
    "handle_owner",
    "handles_locked",
    "heal_member_handles",
    "member_handle",
    "organization_handles",
]
