"""Surreal-backed MCP OAuth authorization flow state.

An MCP OAuth authorization spans several requests (/authorize, the login and
organization pages, then /token), and behind a load balancer each one can land
on a different API replica. The flow therefore lives in one auth-store row
that moves through ``pending`` -> ``authenticated`` -> ``code_issued`` and is
deleted when its code is exchanged. Every transition is a single conditional
statement, so concurrent requests for one flow serialize on the row: a request
issues at most one code, and a code is exchanged at most once.

The request key and the code are bearer values; only their hashes are stored.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sibyl.persistence.surreal.auth_runtime._common import QueryClient, _auth_client_scope
from sibyl_core.backends.surreal.records import (
    SurrealRecord,
    coerce_datetime as _coerce_datetime,
    coerce_uuid as _coerce_uuid,
    normalize_records as _normalize_records,
    utcnow as _utcnow,
)

logger = logging.getLogger(__name__)

type AuthClientScope = Callable[[], AbstractAsyncContextManager[QueryClient]]

_OPEN_STATUSES = ("pending", "authenticated")
_ISSUE_CODE = """
    UPDATE oauth_authorization_requests SET
        status = 'code_issued',
        code_hash = $code_hash,
        user_id = $user_id,
        organization_id = $organization_id,
        authenticated_expires_at = NONE,
        expires_at = $code_expires_at,
        updated_at = $now
    WHERE request_key_hash = $request_key_hash AND expires_at > $now AND {condition}
    RETURN AFTER;
"""
_ISSUE_CODE_FROM_OPEN = _ISSUE_CODE.format(condition="status IN $open_statuses")
_ISSUE_CODE_FROM_LOGIN = _ISSUE_CODE.format(
    condition=(
        "status = 'authenticated' AND user_id = $user_id AND authenticated_expires_at > $now"
    )
)


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationRecord:
    """One authorization flow as stored; never carries the request key or code."""

    client_id: str
    status: str
    state: str | None
    scopes: list[str] | None
    code_challenge: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    resource: str | None
    user_id: UUID | None
    organization_id: UUID | None
    authenticated_expires_at: datetime | None
    expires_at: datetime

    def authenticated_user(self, *, now: datetime) -> UUID | None:
        """The user who logged in for this flow, while that login is still fresh."""
        if self.status != "authenticated" or self.user_id is None:
            return None
        if self.authenticated_expires_at is None or self.authenticated_expires_at <= now:
            return None
        return self.user_id


def _optional_uuid(value: object, *, field_name: str) -> UUID | None:
    if value is None or value == "":
        return None
    return _coerce_uuid(value, field_name=field_name)


def _record_from_row(row: SurrealRecord) -> OAuthAuthorizationRecord:
    expires_at = _coerce_datetime(row.get("expires_at"))
    if expires_at is None:
        msg = "oauth_authorization_requests row is missing expires_at"
        raise ValueError(msg)
    scopes = row.get("scopes")
    return OAuthAuthorizationRecord(
        client_id=str(row.get("client_id") or ""),
        status=str(row.get("status") or "pending"),
        state=str(row["state"]) if row.get("state") is not None else None,
        scopes=[str(scope) for scope in scopes] if isinstance(scopes, list) else None,
        code_challenge=str(row.get("code_challenge") or ""),
        redirect_uri=str(row.get("redirect_uri") or ""),
        redirect_uri_provided_explicitly=bool(row.get("redirect_uri_provided_explicitly", True)),
        resource=str(row["resource"]) if row.get("resource") is not None else None,
        user_id=_optional_uuid(row.get("user_id"), field_name="oauth.user_id"),
        organization_id=_optional_uuid(
            row.get("organization_id"), field_name="oauth.organization_id"
        ),
        authenticated_expires_at=_coerce_datetime(row.get("authenticated_expires_at")),
        expires_at=expires_at,
    )


def _first_record(result: object) -> OAuthAuthorizationRecord | None:
    rows = _normalize_records(result)
    return _record_from_row(rows[0]) if rows else None


class SurrealOAuthAuthorizationStore:
    """Authorization flow state shared by every replica through the auth store."""

    def __init__(self, client_scope: AuthClientScope | None = None) -> None:
        self._client_scope: AuthClientScope = client_scope or _auth_client_scope

    async def create_request(
        self,
        *,
        request_key: str,
        client_id: str,
        state: str | None,
        scopes: list[str] | None,
        code_challenge: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        resource: str | None,
        expires_at: datetime,
    ) -> None:
        now = _utcnow()
        record: SurrealRecord = {
            "uuid": str(uuid4()),
            "request_key_hash": _key_hash(request_key),
            "client_id": client_id,
            "status": "pending",
            "state": state,
            "scopes": list(scopes) if scopes is not None else None,
            "code_challenge": code_challenge,
            "redirect_uri": redirect_uri,
            "redirect_uri_provided_explicitly": redirect_uri_provided_explicitly,
            "resource": resource,
            "expires_at": _coerce_datetime(expires_at),
            "created_at": now,
            "updated_at": now,
        }
        async with self._client_scope() as client:
            created = _normalize_records(
                await client.execute_query(
                    "CREATE oauth_authorization_requests CONTENT $record;", record=record
                )
            )
            if not created:
                msg = "Failed to create OAuth authorization request"
                raise RuntimeError(msg)
            await self._purge_expired(client, now=now)

    async def load_request(
        self, request_key: str, *, now: datetime | None = None
    ) -> OAuthAuthorizationRecord | None:
        """The open (not yet code-issuing) flow for a request key, if unexpired."""
        async with self._client_scope() as client:
            return _first_record(
                await client.execute_query(
                    "SELECT * FROM oauth_authorization_requests "
                    "WHERE request_key_hash = $request_key_hash "
                    "AND status IN $open_statuses AND expires_at > $now LIMIT 1;",
                    request_key_hash=_key_hash(request_key),
                    open_statuses=list(_OPEN_STATUSES),
                    now=now or _utcnow(),
                )
            )

    async def mark_authenticated(
        self,
        request_key: str,
        *,
        user_id: UUID,
        authenticated_expires_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        current = now or _utcnow()
        async with self._client_scope() as client:
            updated = _normalize_records(
                await client.execute_query(
                    "UPDATE oauth_authorization_requests SET "
                    "status = 'authenticated', user_id = $user_id, "
                    "authenticated_expires_at = $authenticated_expires_at, "
                    "updated_at = $now "
                    "WHERE request_key_hash = $request_key_hash "
                    "AND status IN $open_statuses AND expires_at > $now;",
                    request_key_hash=_key_hash(request_key),
                    open_statuses=list(_OPEN_STATUSES),
                    user_id=str(user_id),
                    authenticated_expires_at=_coerce_datetime(authenticated_expires_at),
                    now=current,
                )
            )
        return bool(updated)

    async def issue_code(
        self,
        request_key: str,
        *,
        code: str,
        user_id: UUID,
        organization_id: UUID,
        code_expires_at: datetime,
        require_authenticated: bool,
        now: datetime | None = None,
    ) -> OAuthAuthorizationRecord | None:
        """Turn an open flow into an issued code, at most once per flow.

        With ``require_authenticated`` the flow must still hold a fresh login
        by ``user_id``; the organization picker issues codes that way.
        """
        query = _ISSUE_CODE_FROM_LOGIN if require_authenticated else _ISSUE_CODE_FROM_OPEN
        async with self._client_scope() as client:
            return _first_record(
                await client.execute_query(
                    query,
                    request_key_hash=_key_hash(request_key),
                    open_statuses=list(_OPEN_STATUSES),
                    code_hash=_key_hash(code),
                    user_id=str(user_id),
                    organization_id=str(organization_id),
                    code_expires_at=_coerce_datetime(code_expires_at),
                    now=now or _utcnow(),
                )
            )

    async def load_code(
        self, code: str, *, now: datetime | None = None
    ) -> OAuthAuthorizationRecord | None:
        async with self._client_scope() as client:
            return _first_record(
                await client.execute_query(
                    "SELECT * FROM oauth_authorization_requests "
                    "WHERE code_hash = $code_hash AND status = 'code_issued' "
                    "AND expires_at > $now LIMIT 1;",
                    code_hash=_key_hash(code),
                    now=now or _utcnow(),
                )
            )

    async def consume_code(
        self, code: str, *, now: datetime | None = None
    ) -> OAuthAuthorizationRecord | None:
        """Delete an issued code and return it, or None if another exchange won.

        The DELETE is the whole claim: when two exchanges race, the row goes
        to whichever commits first and the other deletes nothing.
        """
        async with self._client_scope() as client:
            return _first_record(
                await client.execute_query(
                    "DELETE oauth_authorization_requests "
                    "WHERE code_hash = $code_hash AND status = 'code_issued' "
                    "AND expires_at > $now RETURN BEFORE;",
                    code_hash=_key_hash(code),
                    now=now or _utcnow(),
                )
            )

    async def _purge_expired(self, client: QueryClient, *, now: datetime) -> None:
        # Opportunistic: reads already ignore expired rows, so a purge that
        # loses a race with another replica's purge changes nothing.
        try:
            await client.execute_query(
                "DELETE oauth_authorization_requests WHERE expires_at <= $now;", now=now
            )
        except Exception as exc:
            logger.debug("Skipped expired OAuth authorization purge: %s", exc)


__all__ = [
    "OAuthAuthorizationRecord",
    "SurrealOAuthAuthorizationStore",
]
