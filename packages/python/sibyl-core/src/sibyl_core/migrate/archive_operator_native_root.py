"""Inert recovery capture binding for privileged native operator tooling.

Integrity proves bytes, never operator origin. Parsing this envelope grants no
restore, migration, schema installation or credential import permission.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sibyl_core.migrate.archive_native_values import (
    PreparedArchiveNativeValue,
    validate_archive_native_envelope,
)

PROFILE = "operator-approved-deployment-scopes-v1"
_DOMAIN = b"sibyl-archive-operator-native-root-v1\0"


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _catalog_map(value: object, field: str, *, projected: bool = False) -> dict[str, str]:
    if type(value) is not dict or (projected and set(value) != {field}):
        raise ValueError("invalid native recovery catalog object")
    value = cast(dict[str, Any], value)
    members = value.get(field)
    if type(members) is not dict or any(
        type(key) is not str or not key or type(definition) is not str or not definition
        for key, definition in members.items()
    ):
        raise ValueError("native recovery catalog membership must map text names to definitions")
    return cast(dict[str, str], members)


def _validated(payload: object) -> dict[str, Any]:
    if type(payload) is not dict or set(payload) != {"version", "profile", "capture", "sha256"}:
        raise ValueError("unexpected operator recovery envelope fields")
    payload = cast(dict[str, Any], payload)
    if (
        type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["profile"] != PROFILE
    ):
        raise ValueError("unsupported operator recovery capture profile")
    native = validate_archive_native_envelope(payload["capture"])
    root = native.value
    if type(root) is not dict or set(root) != {
        "profile",
        "endpoint",
        "operator_id",
        "decision_id",
        "server_version",
        "graph_namespace_prefix",
        "namespace_catalog",
        "scopes",
    }:
        raise ValueError("invalid qualified native recovery root")
    root = cast(dict[str, Any], root)
    if root["profile"] != PROFILE:
        raise ValueError("native root profile differs from envelope")
    for key in (
        "endpoint",
        "operator_id",
        "decision_id",
        "server_version",
        "graph_namespace_prefix",
    ):
        if type(root[key]) is not str or not root[key]:
            raise ValueError("native recovery identity must be nonempty text")
    if (
        type(root["namespace_catalog"]) is not dict
        or type(root["scopes"]) is not list
        or not root["scopes"]
    ):
        raise ValueError("native recovery root requires qualified catalog scopes")
    namespaces = _catalog_map(root["namespace_catalog"], "namespaces", projected=True)
    stores = [scope.get("store") for scope in root["scopes"] if type(scope) is dict]
    if (
        set(stores) != {"graph", "content", "auth"}
        or stores.count("auth") != 1
        or stores.count("content") != 1
    ):
        raise ValueError("recovery profile requires approved graph and shared auth/content scopes")
    locators: set[tuple[str, str]] = set()
    for scope in root["scopes"]:
        if type(scope) is not dict or set(scope) != {
            "store",
            "namespace",
            "database",
            "organization_id",
            "namespace_catalog",
            "database_catalog",
            "absent_diagnostics",
            "tables",
        }:
            raise ValueError("invalid native recovery scope")
        scope = cast(dict[str, Any], scope)
        if scope["store"] not in ("graph", "content", "auth"):
            raise ValueError("unsupported recovery store")
        for key in ("namespace", "database"):
            if type(scope[key]) is not str or not scope[key]:
                raise ValueError("recovery locator must be nonempty text")
        if scope["store"] == "graph":
            org = scope["organization_id"]
            if type(org) is not str or str(UUID(org)) != org:
                raise ValueError("recovery graph organization must be canonical")
            if scope["namespace"] != root["graph_namespace_prefix"] + UUID(org).hex:
                raise ValueError("recovery graph namespace differs from its bound organization")
        elif scope["organization_id"] is not None:
            raise ValueError("shared recovery scopes are full operator scopes")
        locator = (scope["namespace"], scope["database"])
        if locator in locators:
            raise ValueError("duplicate native recovery scope")
        locators.add(locator)
        if (
            type(scope["namespace_catalog"]) is not dict
            or type(scope["database_catalog"]) is not dict
        ):
            raise ValueError("recovery catalogs must be objects")
        databases = _catalog_map(scope["namespace_catalog"], "databases", projected=True)
        table_members = _catalog_map(scope["database_catalog"], "tables")
        absent = scope["absent_diagnostics"]
        if type(absent) is not list or absent not in ([], ["schema_lease"]):
            raise ValueError("unsupported absent diagnostic table")
        if set(absent) & set(table_members):
            raise ValueError("absent diagnostic is present in native catalog")
        if scope["namespace"] not in namespaces or scope["database"] not in databases:
            raise ValueError("recovery scope is missing from its bound native catalog")
        tables = scope["tables"]
        if type(tables) is not list:
            raise ValueError("recovery table inventory must be an array")
        names: set[str] = set()
        for table in tables:
            if type(table) is not dict or set(table) != {"name", "catalog", "rows"}:
                raise ValueError("invalid native recovery table inventory")
            table = cast(dict[str, Any], table)
            name = table["name"]
            if type(name) is not str or not name or name in names:
                raise ValueError("duplicate or invalid native recovery table")
            names.add(name)
            if type(table["catalog"]) is not dict or type(table["rows"]) is not list:
                raise ValueError("invalid native recovery catalog or row bag")
        if names != set(table_members):
            raise ValueError("recovery table bag differs from native catalog membership")
    unsigned = {key: value for key, value in payload.items() if key != "sha256"}
    digest = hashlib.sha256(_DOMAIN + _canonical(unsigned).encode()).hexdigest()
    if type(payload["sha256"]) is not str or payload["sha256"] != digest:
        raise ValueError("operator recovery envelope integrity mismatch")
    return payload


@dataclass(frozen=True, slots=True)
class PreparedArchiveOperatorRoot:
    """Detached inert data, including secrets only on the operator-owned path."""

    payload_json: str

    def __post_init__(self) -> None:
        if type(self.payload_json) is not str:
            raise ValueError("operator root must be serialized JSON")
        _validated(json.loads(self.payload_json))

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    @property
    def native(self) -> PreparedArchiveNativeValue:
        return validate_archive_native_envelope(self.payload["capture"])


def validate_archive_operator_root(payload: object) -> PreparedArchiveOperatorRoot:
    """Validate uploaded data as inert bytes; never recover its asserted authority."""
    try:
        return PreparedArchiveOperatorRoot(_canonical(_validated(payload)))
    except (TypeError, UnicodeError, RecursionError) as error:
        raise ValueError("invalid operator recovery envelope") from error


def prepare_archive_operator_root(
    native: PreparedArchiveNativeValue,
) -> PreparedArchiveOperatorRoot:
    """Bind a completed capture; origin authorization remains outside serialized data."""
    if type(native) is not PreparedArchiveNativeValue:
        raise TypeError("operator root requires a prepared native value")
    unsigned = {"version": 1, "profile": PROFILE, "capture": native.payload}
    digest = hashlib.sha256(_DOMAIN + _canonical(unsigned).encode()).hexdigest()
    return validate_archive_operator_root({**unsigned, "sha256": digest})
