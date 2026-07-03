"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sibyl.persistence.surreal.auth_runtime._common import (
    SurrealRecord,
    _auth_client_scope,
    _coerce_optional_uuid,
    _log_audit_event,
    _SurrealRepository,
)
from sibyl_core.audit import audit_event_matches_resource


async def log_audit_event(
    *,
    action: str,
    user_id: UUID | None,
    organization_id: UUID | None,
    request,
    details: SurrealRecord,
) -> str | None:
    async with _auth_client_scope() as client:
        return await _log_audit_event(
            client,
            action=action,
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details=details,
        )


def _audit_where_clause(
    *,
    organization_id: UUID | str,
    user_id: UUID | str | None,
    action: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[str, SurrealRecord]:
    clauses = ["organization_id = $organization_id"]
    params: SurrealRecord = {"organization_id": str(organization_id)}
    if user_id:
        clauses.append("user_id = $user_id")
        params["user_id"] = str(user_id)
    if action:
        clauses.append("action = $action")
        params["action"] = action
    if start_time:
        clauses.append("created_at >= $start_time")
        params["start_time"] = start_time
    if end_time:
        clauses.append("created_at <= $end_time")
        params["end_time"] = end_time
    return " AND ".join(clauses), params


def _audit_total(row: SurrealRecord | None) -> int:
    if row is None:
        return 0
    value = row.get("total")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if value is None:
        return 0
    try:
        return int(str(value))
    except ValueError:
        return 0


async def list_audit_events(
    *,
    organization_id: UUID | str,
    user_id: UUID | str | None = None,
    action: str | None = None,
    resource: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SurrealRecord], int]:
    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, offset)
    where_clause, params = _audit_where_clause(
        organization_id=organization_id,
        user_id=user_id,
        action=action,
        start_time=start_time,
        end_time=end_time,
    )

    if resource:
        scan_limit = min(max((bounded_limit + bounded_offset + 1) * 5, 200), 5000)
        query = (
            f"SELECT * FROM audit_logs WHERE {where_clause} "  # noqa: S608
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
        async with _auth_client_scope() as client:
            repo = _SurrealRepository(client)
            rows = await repo.select_many(query, **params, scan_limit=scan_limit)
        filtered = [row for row in rows if audit_event_matches_resource(row, resource)]
        return filtered[bounded_offset : bounded_offset + bounded_limit], len(filtered)

    scan_limit = bounded_offset + bounded_limit
    query = (
        f"SELECT * FROM audit_logs WHERE {where_clause} "  # noqa: S608
        "ORDER BY created_at DESC LIMIT $scan_limit;"
    )
    count_query = f"SELECT count() AS total FROM audit_logs WHERE {where_clause} GROUP ALL;"  # noqa: S608
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        rows = await repo.select_many(query, **params, scan_limit=scan_limit)
        count_row = await repo.select_one(count_query, **params)
    return rows[bounded_offset:], _audit_total(count_row)


def _bounded_audit_value(value: object, *, depth: int = 0) -> object:
    if depth >= 3:
        return str(value)[:500]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, Mapping):
        out: SurrealRecord = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 40:
                out["truncated"] = len(value) - 40
                break
            out[str(key)[:80]] = _bounded_audit_value(item, depth=depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        out = [_bounded_audit_value(item, depth=depth + 1) for item in items[:20]]
        if len(items) > 20:
            out.append({"truncated": len(items) - 20})
        return out
    return str(value)[:500]


def _bounded_audit_string(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)[:500]


def _bounded_audit_id_list(values: list[str] | None) -> tuple[list[str], int]:
    if not values:
        return [], 0
    out = [str(value)[:200] for value in values[:20]]
    return out, max(len(values) - 20, 0)


_MEMORY_AUDIT_ACTION_PREFIX = "memory."
_MEMORY_AUDIT_ACTION_CEILING = "memory/"


async def log_memory_audit_event(
    *,
    action: str,
    user_id: UUID | str | None,
    organization_id: UUID | str | None,
    request,
    memory_scope: str | None = None,
    scope_key: str | None = None,
    project_id: str | None = None,
    source_surface: str | None = None,
    source_ids: list[str] | None = None,
    derived_ids: list[str] | None = None,
    policy_allowed: bool | None = None,
    policy_reason: str | None = None,
    details: Mapping[str, object] | None = None,
) -> str | None:
    """Record metadata-only memory audit receipts exposed through inspect APIs."""
    bounded_source_ids, source_ids_truncated = _bounded_audit_id_list(source_ids)
    bounded_derived_ids, derived_ids_truncated = _bounded_audit_id_list(derived_ids)
    payload: SurrealRecord = {
        "memory_scope": _bounded_audit_string(memory_scope),
        "scope_key": _bounded_audit_string(scope_key),
        "project_id": _bounded_audit_string(project_id),
        "source_surface": _bounded_audit_string(source_surface),
        "source_ids": bounded_source_ids,
        "derived_ids": bounded_derived_ids,
        "policy_allowed": policy_allowed,
        "policy_reason": _bounded_audit_string(policy_reason),
    }
    if source_ids_truncated:
        payload["source_ids_truncated"] = source_ids_truncated
    if derived_ids_truncated:
        payload["derived_ids_truncated"] = derived_ids_truncated
    if details:
        payload["details"] = _bounded_audit_value(details)

    async with _auth_client_scope() as client:
        return await _log_audit_event(
            client,
            action=action,
            user_id=_coerce_optional_uuid(user_id),
            organization_id=_coerce_optional_uuid(organization_id),
            request=request,
            details=payload,
        )


def _memory_audit_details(row: Mapping[str, object]) -> Mapping[str, object]:
    details = row.get("details")
    if isinstance(details, Mapping):
        return {str(key): value for key, value in details.items()}
    return {}


def _memory_audit_id_matches(details: Mapping[str, object], key: str, value: str | None) -> bool:
    if not value:
        return True
    ids = details.get(key)
    if not isinstance(ids, list):
        return False
    return value in {str(item) for item in ids}


def _memory_audit_row_matches(
    row: Mapping[str, object],
    *,
    action: str | None,
    source_id: str | None,
    derived_id: str | None,
    memory_scope: str | None,
    project_id: str | None,
    policy_allowed: bool | None,
) -> bool:
    action_value = str(row.get("action") or "")
    if not action_value.startswith("memory."):
        return False
    if action and action_value != action:
        return False
    details = _memory_audit_details(row)
    if memory_scope and details.get("memory_scope") != memory_scope:
        return False
    if project_id and details.get("project_id") != project_id:
        return False
    if policy_allowed is not None and details.get("policy_allowed") != policy_allowed:
        return False
    if not _memory_audit_id_matches(details, "source_ids", source_id):
        return False
    return _memory_audit_id_matches(details, "derived_ids", derived_id)


async def list_memory_audit_events(
    *,
    organization_id: UUID | str,
    user_id: UUID | str | None = None,
    action: str | None = None,
    source_id: str | None = None,
    derived_id: str | None = None,
    memory_scope: str | None = None,
    project_id: str | None = None,
    policy_allowed: bool | None = None,
    limit: int = 50,
) -> list[SurrealRecord]:
    if action and not action.startswith(_MEMORY_AUDIT_ACTION_PREFIX):
        return []

    bounded_limit = max(1, min(limit, 200))
    scan_limit = max(100, min(bounded_limit * 5, 500))
    params: SurrealRecord = {
        "organization_id": str(organization_id),
        "scan_limit": scan_limit,
    }
    if user_id:
        params["user_id"] = str(user_id)
    if action:
        params["action"] = action
    else:
        params["memory_action_prefix"] = _MEMORY_AUDIT_ACTION_PREFIX
        params["memory_action_ceiling"] = _MEMORY_AUDIT_ACTION_CEILING

    if user_id and action:
        query = (
            "SELECT * FROM audit_logs "
            "WHERE organization_id = $organization_id "
            "AND user_id = $user_id "
            "AND action = $action "
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
    elif user_id:
        query = (
            "SELECT * FROM audit_logs "
            "WHERE organization_id = $organization_id "
            "AND user_id = $user_id "
            "AND action >= $memory_action_prefix "
            "AND action < $memory_action_ceiling "
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
    elif action:
        query = (
            "SELECT * FROM audit_logs "
            "WHERE organization_id = $organization_id "
            "AND action = $action "
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
    else:
        query = (
            "SELECT * FROM audit_logs "
            "WHERE organization_id = $organization_id "
            "AND action >= $memory_action_prefix "
            "AND action < $memory_action_ceiling "
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        rows = await repo.select_many(query, **params)

    events: list[SurrealRecord] = []
    for row in rows:
        if _memory_audit_row_matches(
            row,
            action=action,
            source_id=source_id,
            derived_id=derived_id,
            memory_scope=memory_scope,
            project_id=project_id,
            policy_allowed=policy_allowed,
        ):
            events.append(row)
        if len(events) >= bounded_limit:
            break
    return events
