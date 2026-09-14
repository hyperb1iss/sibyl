"""Execution-scoped encrypted receipts for logical content archives."""

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import InvalidToken

from sibyl_core.services import validation_receipts
from sibyl_core.services.validation_dependencies import (
    dependency_closure,
    normalize_legacy_dependencies,
)
from sibyl_core.services.validation_execution import validation_archive_guard
from sibyl_core.services.validation_progress_history import (
    ProgressHistoryBinding,
    validate_history_row,
    validate_progress_assessments,
)
from sibyl_core.services.validation_result_codec import (
    decode_validation_result,
    validate_result_request,
)
from sibyl_core.tasks._evidence_json import canonical


def _rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        validation_archive_guard("memory_validation_executions", row)
        identity = row["uuid"]
        if identity in result:
            raise ValueError("Duplicate validation archive execution")
        result[identity] = row
    result = {row["uuid"]: row for row in normalize_legacy_dependencies(list(result.values()))}
    validated: dict[str, list[str]] = {}
    for row in result.values():
        if not row["purged"]:
            expected = dependency_closure(
                json.loads(row["request_json"]),
                result,
                execution_id=row["uuid"],
                org=row["organization_id"],
                principal=row["principal_id"],
                validated=validated,
            )
            if row.get("dependency_ids", []) != expected:
                raise ValueError("Validation archive dependency inventory differs")
        history = json.loads(row["request_json"]).get("progress_history")
        if history is not None and not row["purged"]:
            binding = ProgressHistoryBinding.model_validate(history)
            prior = result.get(binding.execution_id)
            if prior is None or binding.execution_id == row["uuid"]:
                raise ValueError("Progress archive prior execution is unavailable")
            critique = validate_history_row(
                prior, binding, row["organization_id"], row["principal_id"]
            )
            if row.get("result_json") is not None:
                validate_progress_assessments(
                    decode_validation_result(json.loads(row["result_json"])), critique
                )
    return result


def _status(
    row: dict[str, Any], ciphertext: bytes | None, records: dict[str, dict[str, Any]]
) -> str:
    if row["purged"]:
        if row.get("recovery_key") is not None:
            raise ValueError("Purged validation archive retains a recovery key")
        return "purged"
    if ciphertext is not None:
        try:
            value = validation_receipts.decode(row["request_json"], row["recovery_key"], ciphertext)
        except InvalidToken:
            raise ValueError("Validation archive ciphertext authentication failed") from None
        decoded = decode_validation_result(value)
        request = json.loads(row["request_json"])
        validate_result_request(decoded, request)
        if request.get("progress_history") is not None:
            binding = ProgressHistoryBinding.model_validate(request["progress_history"])
            prior = validate_history_row(
                records[binding.execution_id], binding, row["organization_id"], row["principal_id"]
            )
            validate_progress_assessments(decoded, prior)
        if row.get("result_json") is not None and canonical(value) != row["result_json"]:
            raise ValueError("Validation archive receipt and database result differ")
        if row.get("usage_json") is not None and canonical(value["usage"]) != row["usage_json"]:
            raise ValueError("Validation archive receipt and database usage differ")
        return "journal"
    if row.get("result_json") is not None:
        value = json.loads(row["result_json"])
        if canonical(value["usage"]) != row.get("usage_json"):
            raise ValueError("Validation archive database usage differs")
        return "database"
    return "unresolved"


def capture(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Capture only files named by this authorized execution snapshot."""
    entries = []
    records = _rows(rows)
    for identity, row in sorted(records.items()):
        ciphertext = None
        if not row["purged"] and row.get("recovery_key") is not None:
            ciphertext = validation_receipts.capture(row["request_json"])
        entry = {"execution_id": identity, "status": _status(row, ciphertext, records)}
        if ciphertext is not None:
            entry.update(
                ciphertext=base64.b64encode(ciphertext).decode("ascii"),
                sha256=hashlib.sha256(ciphertext).hexdigest(),
            )
        entries.append(entry)
    return {"version": "1.0", "executions": entries}


def prepare(section: Any, rows: list[dict[str, Any]]) -> list[tuple[str, str, bytes]]:
    """Validate the entire section before publishing any destination ciphertext."""
    records = _rows(rows)
    if not isinstance(section, dict) or set(section) != {"version", "executions"}:
        raise ValueError("Validation receipt archive section is missing or malformed")
    entries = section["executions"]
    if section["version"] != "1.0" or not isinstance(entries, list):
        raise ValueError("Unsupported validation receipt archive")
    pending = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Validation receipt archive entry must be an object")
        identity = entry.get("execution_id")
        if not isinstance(identity, str) or identity not in records or identity in seen:
            raise ValueError("Validation receipt archive execution differs")
        seen.add(identity)
        row = records[identity]
        ciphertext = None
        fields = {"execution_id", "status"}
        if entry.get("status") == "journal":
            fields |= {"ciphertext", "sha256"}
            if not isinstance(entry.get("ciphertext"), str):
                raise ValueError("Validation receipt archive ciphertext is missing")
            ciphertext = base64.b64decode(entry["ciphertext"], validate=True)
            if hashlib.sha256(ciphertext).hexdigest() != entry.get("sha256"):
                raise ValueError("Validation receipt archive checksum differs")
            if row["purged"] or not isinstance(row.get("recovery_key"), str):
                raise ValueError("Validation receipt archive key is unavailable")
        if set(entry) != fields or entry.get("status") != _status(row, ciphertext, records):
            raise ValueError("Validation receipt archive status differs")
        if ciphertext is not None:
            pending.append((row["request_json"], row["recovery_key"], ciphertext))
    if seen != set(records):
        raise ValueError("Validation receipt archive execution inventory differs")
    return pending


def publish(pending: list[tuple[str, str, bytes]]) -> None:
    """Publish before the database transaction; interrupted imports remain replayable."""
    for request, _key, ciphertext in pending:
        existing = validation_receipts.capture(request)
        if existing is not None and existing != ciphertext:
            raise ValueError("Validation receipt archive conflicts with local history")
    for request, key, ciphertext in pending:
        validation_receipts.restore(request, key, ciphertext)


def prepare_payload(payload: dict[str, Any]) -> list[tuple[str, str, bytes]]:
    """Apply the receipt format contract at both public validation and restore."""
    if payload.get("version") == "2.3":
        tables = payload.get("tables")
        rows = tables.get("memory_validation_executions") if isinstance(tables, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Validation receipt archive execution table is missing")
        integrity = payload.get("source_integrity", {})
        if isinstance(integrity, dict):
            from uuid import NAMESPACE_URL, uuid5

            from sibyl_core.services.validation_origin import validate_origin_row

            executions = {row["uuid"]: row for row in rows}
            derivations = integrity.get("derivations", [])
            for derivation in derivations:
                if derivation.get("origin_execution_id") is not None:
                    validate_origin_row(
                        derivation,
                        executions.get(derivation["origin_execution_id"]),
                        historical=True,
                    )
            # A retained packet-producing execution proves this candidate is not
            # legacy, even when its mutable metadata and origin field were erased.
            packet_executions = set()
            requests = {
                identity: json.loads(row["request_json"]) for identity, row in executions.items()
            }
            while True:
                previous = set(packet_executions)
                for identity, request in requests.items():
                    if request.get("evidence_packet") is not None or any(
                        dependency.get("execution_id") in packet_executions
                        for dependency in request.get("execution_dependencies", [])
                    ):
                        packet_executions.add(identity)
                if previous == packet_executions:
                    break
            by_target = {item["target_id"]: item for item in derivations}
            captures = {item["record"]["uuid"] for item in integrity.get("source_rows", [])}
            for identity in packet_executions:
                target = str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + identity))
                if (
                    target in captures
                    and by_target.get(target, {}).get("origin_execution_id") != identity
                ):
                    raise ValueError("Packet archive candidate omitted its protected origin")
        return prepare(payload.get("validation_receipts"), rows)
    if "validation_receipts" in payload:
        raise ValueError("Legacy content archive cannot carry validation receipts")
    integrity = payload.get("source_integrity", {})
    if isinstance(integrity, dict) and any(
        row.get("origin_execution_id") is not None for row in integrity.get("derivations", [])
    ):
        raise ValueError("Derived execution origins require the validation receipt archive")
    return []
