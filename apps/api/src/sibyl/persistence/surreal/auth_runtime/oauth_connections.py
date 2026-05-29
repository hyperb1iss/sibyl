"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException

from sibyl.persistence.surreal.auth_runtime._common import (
    _auth_client_scope,
    _oauth_connection_namespace,
    _record_payload,
    _SurrealRepository,
)
from sibyl_core.backends.surreal.records import (
    normalize_record as _normalize_record,
    normalize_records as _normalize_records,
)


async def list_oauth_connections(*, user_id: UUID) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        rows = await repo.select_many(
            "SELECT * FROM oauth_connections WHERE user_id = $user_id ORDER BY created_at ASC;",
            user_id=str(user_id),
        )
        return [row for record in rows if (row := _oauth_connection_namespace(record)) is not None]


async def remove_oauth_connection(
    *,
    user_id: UUID,
    connection_id: UUID,
):
    async with _auth_client_scope() as client:
        payload = await client.execute_query(
            """
                RETURN {
                    connection: (
                        SELECT * FROM oauth_connections
                        WHERE uuid = $connection_id AND user_id = $user_id
                        LIMIT 1
                    )[0],
                    user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                    connections: (
                        SELECT * FROM oauth_connections
                        WHERE user_id = $user_id
                        ORDER BY created_at ASC
                    ),
                };
            """,
            connection_id=str(connection_id),
            user_id=str(user_id),
        )
        payload = _record_payload(payload)
        connection = _oauth_connection_namespace(_normalize_record(payload.get("connection")))
        if connection is None:
            raise HTTPException(status_code=404, detail="Connection not found")

        user = _normalize_record(payload.get("user"))
        remaining_connections = _normalize_records(payload.get("connections"))
        has_other_connections = any(
            str(row.get("uuid")) != str(connection_id) for row in remaining_connections
        )
        has_password = bool(user and user.get("password_hash"))
        if not has_other_connections and not has_password:
            raise HTTPException(
                status_code=400,
                detail="Cannot remove last login method. Set a password first.",
            )

        await client.execute_query(
            "DELETE FROM oauth_connections WHERE uuid = $uuid;",
            uuid=str(connection_id),
        )
        return connection
