"""Legacy request-time auth runtime helpers backed by Postgres."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException

from sibyl.auth.audit import AuditLogger
from sibyl.auth.sessions import SessionManager
from sibyl.auth.users import UserManager
from sibyl.db.connection import get_session
from sibyl.db.project_sync import sync_project_create, sync_project_delete, sync_project_update
from sibyl.persistence.legacy.users import (
    confirm_legacy_password_reset as legacy_confirm_password_reset,
    list_legacy_oauth_connections as legacy_list_oauth_connections,
    remove_legacy_oauth_connection as legacy_remove_oauth_connection,
    request_legacy_password_reset as legacy_request_password_reset,
)


async def patch_legacy_auth_user(
    *,
    user_id: UUID,
    updates: dict[str, Any],
    organization_id: UUID | None,
    request: Any,
):
    async with get_session() as session:
        manager = UserManager(session)
        user = await manager.get_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        changes: list[str] = []
        profile_updates: dict[str, Any] = {}
        if "email" in updates:
            profile_updates["email"] = updates["email"]
            changes.append("email")
        if "name" in updates:
            profile_updates["name"] = updates["name"]
            changes.append("name")
        if "avatar_url" in updates:
            profile_updates["avatar_url"] = updates["avatar_url"]
            changes.append("avatar_url")

        try:
            if profile_updates:
                await manager.update_profile(user, **profile_updates)
            if "bio" in updates:
                user.bio = str(updates["bio"]).strip() or None if updates["bio"] is not None else None
                changes.append("bio")
            if "timezone" in updates:
                timezone = updates["timezone"]
                user.timezone = str(timezone).strip() or "UTC" if timezone is not None else "UTC"
                changes.append("timezone")
            if "preferences" in updates:
                preferences = updates["preferences"]
                if not isinstance(preferences, dict):
                    raise ValueError("Preferences must be an object")
                user.preferences = dict(preferences)
                changes.append("preferences")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not changes:
            raise HTTPException(status_code=400, detail="No fields to update")

        await AuditLogger(session).log(
            action="user.update_profile",
            user_id=user.id,
            organization_id=organization_id,
            request=request,
            details={"fields": changes},
        )
        return user


async def create_legacy_project_record(
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_project_id: str,
    name: str,
    description: str | None = None,
):
    async with get_session() as session:
        return await sync_project_create(
            session,
            organization_id=organization_id,
            owner_user_id=owner_user_id,
            graph_project_id=graph_project_id,
            name=name,
            description=description,
        )


async def update_legacy_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    async with get_session() as session:
        return await sync_project_update(
            session,
            organization_id=organization_id,
            graph_project_id=graph_project_id,
            name=name,
            description=description,
        )


async def delete_legacy_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> bool:
    async with get_session() as session:
        return await sync_project_delete(
            session,
            organization_id=organization_id,
            graph_project_id=graph_project_id,
        )


async def list_legacy_user_sessions(
    *,
    user_id: UUID,
    include_expired: bool = False,
):
    async with get_session() as session:
        manager = SessionManager(session)
        return await manager.list_user_sessions(user_id, include_expired=include_expired)


async def revoke_all_legacy_user_sessions(
    *,
    user_id: UUID,
    exclude_token_hash: str | None = None,
) -> int:
    async with get_session() as session:
        manager = SessionManager(session)
        return await manager.revoke_all_sessions(user_id, exclude_token_hash=exclude_token_hash)


async def revoke_legacy_user_session(
    *,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    async with get_session() as session:
        manager = SessionManager(session)
        return await manager.revoke_session(session_id, user_id)


async def request_legacy_password_reset(email: str) -> None:
    await legacy_request_password_reset(email)


async def confirm_legacy_password_reset(token: str, new_password: str) -> None:
    await legacy_confirm_password_reset(token, new_password)


async def list_legacy_oauth_connections(*, user_id: UUID):
    async with get_session() as session:
        return await legacy_list_oauth_connections(session, user_id)


async def remove_legacy_oauth_connection(
    *,
    user_id: UUID,
    connection_id: UUID,
):
    async with get_session() as session:
        return await legacy_remove_oauth_connection(
            session,
            user_id=user_id,
            connection_id=connection_id,
        )
