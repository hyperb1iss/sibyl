"""Complete, typed source provenance sections for privileged application archives."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from typing import Any

from sibyl_core.memory_pipeline.observations import SourceKind

INTEGRITY_ARCHIVE_VERSION = 1


def encode_record(record: dict[str, Any]) -> dict[str, Any]:
    """Preserve database datetimes without interpreting user strings as dates."""
    datetimes: list[list[str | int]] = []

    def encode(value: Any, path: list[str | int]) -> Any:
        if isinstance(value, datetime):
            datetimes.append(path)
            return value.isoformat()
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("archive record keys must be strings")
            return {key: encode(item, [*path, key]) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [encode(item, [*path, index]) for index, item in enumerate(value)]
        if value is None or isinstance(value, str | int | float | bool):
            return value
        raise ValueError(f"unsupported archive value type: {type(value).__name__}")

    return {"record": encode(record, []), "datetimes": datetimes}


def decode_record(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"record", "datetimes"}:
        raise ValueError("archive source row requires record and datetimes")
    if not isinstance(payload["record"], dict) or not isinstance(payload["datetimes"], list):
        raise ValueError("invalid typed archive record")
    record = deepcopy(payload["record"])
    for path in payload["datetimes"]:
        if not isinstance(path, list) or not path:
            raise ValueError("invalid archive datetime path")
        parent: Any = record
        for key in path[:-1]:
            if type(key) not in (str, int):
                raise ValueError("invalid archive datetime path component")
            parent = parent[key]
        key = path[-1]
        if type(key) not in (str, int) or not isinstance(parent[key], str):
            raise ValueError("archive datetime path must identify a string")
        parent[key] = datetime.fromisoformat(parent[key])
    return record


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def build_integrity_archive(
    *,
    kind: SourceKind,
    organizations: list[str],
    source_rows: list[dict[str, Any]],
    source_states: list[dict[str, Any]],
    derivations: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "version": INTEGRITY_ARCHIVE_VERSION,
        "kind": kind.value,
        "organizations": sorted(organizations),
        "source_rows": [encode_record(row) for row in source_rows],
        "source_states": source_states,
        "derivations": derivations,
    }
    payload["sha256"] = _digest(payload)
    validate_integrity_archive(payload, kind=kind, organizations=organizations)
    return payload


def validate_integrity_archive(
    payload: object, *, kind: SourceKind, organizations: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reject incomplete or foreign provenance before any destination mutation."""
    required = {
        "version",
        "kind",
        "organizations",
        "source_rows",
        "source_states",
        "derivations",
        "sha256",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("archive integrity section is incomplete")
    if (
        type(payload["version"]) is not int
        or payload["version"] != INTEGRITY_ARCHIVE_VERSION
        or payload["kind"] != kind.value
    ):
        raise ValueError("unsupported archive integrity contract")
    if any(not isinstance(org, str) or not org.strip() for org in organizations):
        raise ValueError("archive organization scope must be explicit")
    if payload["organizations"] != sorted(set(organizations)):
        raise ValueError("archive integrity organization mismatch")
    if payload["sha256"] != _digest({k: v for k, v in payload.items() if k != "sha256"}):
        raise ValueError("archive integrity digest mismatch")
    if any(
        not isinstance(payload[key], list)
        for key in ("source_rows", "source_states", "derivations")
    ):
        raise ValueError("archive integrity collections must be arrays")
    rows = [decode_record(row) for row in payload["source_rows"]]
    states = deepcopy(payload["source_states"])
    derivations = deepcopy(payload["derivations"])
    orgs = set(organizations)
    org_field = "group_id" if kind is SourceKind.GRAPH_ENTITY else "organization_id"
    row_keys: set[tuple[str, str]] = set()
    state_keys: set[tuple[str, str]] = set()
    for row in rows:
        org, identity = row.get(org_field), row.get("uuid")
        if org not in orgs or not isinstance(identity, str) or not identity:
            raise ValueError("archive source identity mismatch")
        key = (org, identity)
        if key in row_keys:
            raise ValueError("duplicate archive source identity")
        row_keys.add(key)
        if (
            row.get("derivation_required") is not None
            and type(row["derivation_required"]) is not bool
        ):
            raise ValueError("invalid archive source marker")
    for state in states:
        if not isinstance(state, dict):
            raise ValueError("invalid archive source state")
        org, identity = state.get("organization_id"), state.get("source_id")
        if org not in orgs or not isinstance(identity, str) or not identity:
            raise ValueError("archive ledger identity mismatch")
        if state.get("source_kind") != kind.value:
            raise ValueError("archive ledger source kind mismatch")
        key = (org, identity)
        if key in state_keys:
            raise ValueError("duplicate archive source ledger")
        state_keys.add(key)
        if (
            type(state.get("generation")) is not int
            or state["generation"] <= 0
            or type(state.get("revision")) is not int
            or state["revision"] < 0
            or type(state.get("deleted")) is not bool
            or not isinstance(state.get("incarnation"), str)
            or not state["incarnation"].strip()
        ):
            raise ValueError("invalid archive source incarnation or high-water")
    if not row_keys <= state_keys:
        raise ValueError("archive source has no retained ledger")
    ledger_by_key = {(state["organization_id"], state["source_id"]): state for state in states}
    for row in rows:
        state = ledger_by_key[(row[org_field], row["uuid"])]
        if type(row.get("revision")) is not int or row["revision"] != state["revision"]:
            raise ValueError("archive source revision does not match retained ledger")
    if any(
        not state["deleted"] and (state["organization_id"], state["source_id"]) not in row_keys
        for state in states
    ):
        raise ValueError("archive live ledger has no source row")
    association_keys: set[tuple[str, str]] = set()
    for association in derivations:
        if not isinstance(association, dict):
            raise ValueError("invalid archive derivation")
        key = (association.get("organization_id"), association.get("target_id"))
        if key not in state_keys or association.get("target_kind") != kind.value:
            raise ValueError("archive derivation target has no scoped ledger")
        if key in association_keys:
            raise ValueError("duplicate archive target association")
        association_keys.add(key)
        if type(association.get("active")) is not bool:
            raise ValueError("archive derivation requires explicit retirement state")
        if not isinstance(association.get("observations"), list) or not association["observations"]:
            raise ValueError("archive derivation has no captured observations")
        if (
            not isinstance(association.get("body_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", association["body_sha256"]) is None
            or not isinstance(association.get("principal_id"), str)
            or not association["principal_id"].strip()
            or not isinstance(association.get("authority_ceiling"), dict)
        ):
            raise ValueError("archive derivation body or authority is malformed")
        from sibyl_core.services.memory_derivations import observation_from_record

        for value in association["observations"]:
            observation = observation_from_record(value)
            if not observation.durable or observation.source.organization_id != key[0]:
                raise ValueError("archive derivation source is not scoped durable evidence")
    return rows, states, derivations
