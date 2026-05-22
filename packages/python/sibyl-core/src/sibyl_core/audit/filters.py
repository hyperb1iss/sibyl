"""Helpers for filtering bounded audit payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast


def _details(row: Mapping[str, object]) -> Mapping[str, object]:
    details = row.get("details")
    if isinstance(details, Mapping):
        return cast("Mapping[str, object]", details)
    return {}


def _string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def audit_event_resource(row: Mapping[str, object]) -> str | None:
    details = _details(row)
    for key in ("resource", "resource_id", "project_id", "memory_scope", "scope_key"):
        if resource := _string(details.get(key)):
            return resource
    source_ids = details.get("source_ids")
    if isinstance(source_ids, list) and source_ids:
        return _string(source_ids[0])
    derived_ids = details.get("derived_ids")
    if isinstance(derived_ids, list) and derived_ids:
        return _string(derived_ids[0])
    return None


def audit_event_matches_resource(row: Mapping[str, object], resource: str | None) -> bool:
    needle = (resource or "").strip().lower()
    if not needle:
        return True

    details = _details(row)
    fields = [
        row.get("uuid"),
        row.get("user_id"),
        row.get("organization_id"),
        row.get("action"),
        audit_event_resource(row),
        details.get("resource"),
        details.get("resource_id"),
        details.get("project_id"),
        details.get("memory_scope"),
        details.get("scope_key"),
    ]
    haystack = " ".join(value for item in fields if (value := _string(item)))
    if needle in haystack.lower():
        return True

    payload = json.dumps(details, sort_keys=True, default=str)
    return needle in payload.lower()
