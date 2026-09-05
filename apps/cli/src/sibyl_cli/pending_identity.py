"""Validated, session-independent ownership of buffered mutations."""

from typing import Any
from uuid import UUID


def normalize_replay_identity(value: object) -> dict[str, Any] | None:
    """Accept only the complete server-issued v1 identity contract."""
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        return None
    if value["version"] != 1:
        return None
    identity: dict[str, Any] = {"version": 1}
    for key in ("server_instance_id", "user_id", "organization_id"):
        try:
            identity[key] = str(UUID(str(value[key])))
        except (KeyError, ValueError, TypeError):
            return None
    credential = value.get("credential")
    if not isinstance(credential, dict) or credential.get("kind") not in ("session", "api_key"):
        return None
    normalized: dict[str, Any] = {"kind": credential["kind"]}
    key_id = credential.get("api_key_id")
    if credential["kind"] == "api_key":
        try:
            key_id = str(UUID(str(key_id)))
        except (ValueError, TypeError):
            return None
    elif key_id is not None:
        return None
    normalized["api_key_id"] = key_id
    for key in ("scopes", "project_ids", "memory_space_ids", "memory_scope_keys"):
        if key not in credential:
            return None
        entries = credential[key]
        if entries is None and key != "scopes":
            normalized[key] = None
        elif isinstance(entries, list) and all(isinstance(item, str) for item in entries):
            normalized[key] = sorted(set(entries))
        else:
            return None
    identity["credential"] = normalized
    return identity


def pending_identity_matches(
    item: dict[str, Any], identity: dict[str, Any] | None, replay_scope: str | None
) -> bool:
    """Never fall back to credential lineage when a durable owner is present."""
    if item.get("replay_identity") is not None:
        owner = normalize_replay_identity(item["replay_identity"])
        return owner is not None and identity is not None and owner == identity
    return replay_scope is not None and item.get("replay_scope") == replay_scope
