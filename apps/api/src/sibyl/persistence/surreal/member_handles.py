"""Assigning, renaming and looking up organization member handles.

Every path that creates a membership goes through ``create_membership``,
which picks the member's handle and writes the membership under one lock per
organization, so two members joining at once can never take the same handle.
Renames take the same lock. The lock is the coordination backend's entity
lock, shared across replicas under Redis coordination.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Protocol

from sibyl.locks import entity_lock
from sibyl_core.auth.handles import handle_base, normalize_handle, pick_handle
from sibyl_core.backends.surreal.records import normalize_records

HANDLES_LOCK_ENTITY = "member-handles"


class _Queries(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


class HandleTakenError(ValueError):
    """Another member of the organization already holds the handle."""

    def __init__(self, handle: str) -> None:
        self.handle = handle
        super().__init__(f"@{handle} is already another member's handle")


@asynccontextmanager
async def handles_locked(organization_id: str) -> AsyncGenerator[None]:
    """Hold the organization's handle lock while a handle is chosen and written."""
    async with entity_lock(organization_id, HANDLES_LOCK_ENTITY, blocking=True) as token:
        if not token:
            msg = f"could not lock member handles for organization {organization_id}"
            raise RuntimeError(msg)
        yield


async def _taken(client: _Queries, organization_id: str) -> set[str]:
    return set(await organization_handles(client, organization_id=organization_id))


async def _first(client: _Queries, query: str, field: str, **params: object) -> str | None:
    rows = normalize_records(await client.execute_query(query, **params))
    value = rows[0].get(field) if rows else None
    return str(value) if value else None


async def _identity(client: _Queries, user_id: str) -> tuple[str | None, str | None]:
    rows = normalize_records(
        await client.execute_query(
            "SELECT email, name FROM users WHERE uuid = $uuid LIMIT 1;", uuid=user_id
        )
    )
    user = rows[0] if rows else {}
    email, name = user.get("email"), user.get("name")
    return (str(email) if email else None, str(name) if name else None)


async def first_member_handle(client: _Queries, user_id: str) -> str:
    """The handle of a new organization's first member, where nothing is taken yet."""
    email, name = await _identity(client, user_id)
    return handle_base(email, name)


async def create_membership(client: _Queries, record: Mapping[str, object]) -> object:
    """Create a membership carrying a fresh handle; returns the CREATE's result."""
    organization_id = str(record["organization_id"])
    async with handles_locked(organization_id):
        email, name = await _identity(client, str(record["user_id"]))
        handle = pick_handle(handle_base(email, name), await _taken(client, organization_id))
        return await client.execute_query(
            "CREATE organization_members CONTENT $record;", record={**record, "handle": handle}
        )


async def member_handle(client: _Queries, *, organization_id: str, user_id: str) -> str | None:
    """The handle a member holds in an organization, or None when they are not a member."""
    return await _first(
        client,
        "SELECT handle FROM organization_members "
        "WHERE organization_id = $organization_id AND user_id = $user_id LIMIT 1;",
        "handle",
        organization_id=organization_id,
        user_id=user_id,
    )


async def handle_owner(client: _Queries, *, organization_id: str, handle: str) -> str | None:
    """The user holding ``handle`` in the organization, read on the handle index."""
    return await _first(
        client,
        "SELECT user_id FROM organization_members "
        "WHERE organization_id = $organization_id AND handle = $handle LIMIT 1;",
        "user_id",
        organization_id=organization_id,
        handle=handle,
    )


async def organization_handles(client: _Queries, *, organization_id: str) -> dict[str, str]:
    """Every handle in the organization, with the user holding it."""
    rows = normalize_records(
        await client.execute_query(
            "SELECT user_id, handle FROM organization_members "
            "WHERE organization_id = $organization_id AND handle != NONE;",
            organization_id=organization_id,
        )
    )
    return {str(row["handle"]): str(row.get("user_id") or "") for row in rows if row.get("handle")}


async def rename_member_handle(
    client: _Queries, *, organization_id: str, user_id: str, handle: str
) -> str:
    """Give a member a new handle; ``HandleTakenError`` when another member holds it."""
    wanted = normalize_handle(handle)
    async with handles_locked(organization_id):
        owner = await handle_owner(client, organization_id=organization_id, handle=wanted)
        if owner is not None and owner != user_id:
            raise HandleTakenError(wanted)
        await client.execute_query(
            "UPDATE organization_members SET handle = $handle, updated_at = time::now() "
            "WHERE organization_id = $organization_id AND user_id = $user_id RETURN NONE;",
            handle=wanted,
            organization_id=organization_id,
            user_id=user_id,
        )
    return wanted


__all__ = [
    "HANDLES_LOCK_ENTITY",
    "HandleTakenError",
    "create_membership",
    "first_member_handle",
    "handle_owner",
    "handles_locked",
    "member_handle",
    "organization_handles",
    "rename_member_handle",
]
