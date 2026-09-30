"""Measured, bounded archive intake before eager logical archive validation."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import tarfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from sibyl_core.migrate.archive import (
    ArchiveManifest,
    LoadedArchive,
    validate_archive,
)
from sibyl_core.migrate.archive_lineage import seal_archive_lineage
from sibyl_core.migrate.backup_envelope import load_backup_archive
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveSourceOrigin,
    archive_digest,
    canonical_json,
)


class ArchiveIntakeError(ValueError):
    """The uploaded archive does not satisfy its typed intake contract."""


class ArchiveIntakeCapacityError(ArchiveIntakeError):
    """Measured input exceeds the configured staging resource budget."""


@dataclass(frozen=True)
class ArchiveIntakeBudget:
    compressed_bytes: int
    inflated_bytes: int
    member_bytes: int
    members: int
    json_depth: int
    json_scalar_bytes: int
    json_nodes: int
    parsed_rows: int
    encoded_artifact_bytes: int
    encoded_plan_bytes: int
    metadata_transaction_bytes: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ValueError("archive resource budgets must be positive integers")


@dataclass(frozen=True)
class ParsedPersonalArchive:
    archive: LoadedArchive
    origin: ArchiveSourceOrigin
    archive_sha256: str
    artifact_sha256: str
    member_inventory_json: str
    staged_payload_json: str
    measured_sizes_json: str
    graph: dict[str, object] | None
    content: dict[str, object] | None


class _ReadableBytes(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class _MeasuredInflation:
    def __init__(self, stream: _ReadableBytes, maximum: int) -> None:
        self.stream = stream
        self.maximum = maximum
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        amount = min(65536, size if size >= 0 else 65536, self.maximum - self.bytes_read + 1)
        data = self.stream.read(amount)
        self.bytes_read += len(data)
        if self.bytes_read > self.maximum:
            raise ArchiveIntakeCapacityError("archive inflated-byte budget exceeded")
        return data


def _json_resource_scan(encoded: bytes, budget: ArchiveIntakeBudget) -> int:
    depth = nodes = scalar_bytes = 0
    quoted = escaped = scalar = False
    for character in encoded:
        if quoted:
            scalar_bytes += 1
            if scalar_bytes > budget.json_scalar_bytes:
                raise ArchiveIntakeCapacityError("archive JSON scalar-byte budget exceeded")
            if escaped:
                escaped = False
            elif character == 92:
                escaped = True
            elif character == 34:
                quoted = False
            continue
        if character == 34:
            quoted, scalar, scalar_bytes = True, False, 0
            nodes += 1
        elif character in (91, 123):
            depth += 1
            nodes += 1
            scalar = False
            if depth > budget.json_depth:
                raise ArchiveIntakeCapacityError("archive JSON nesting budget exceeded")
        elif character in (93, 125):
            depth -= 1
            scalar = False
        elif character in (9, 10, 13, 32, 44, 58):
            scalar = False
        else:
            if not scalar:
                scalar, scalar_bytes = True, 0
                nodes += 1
            scalar_bytes += 1
            if scalar_bytes > budget.json_scalar_bytes:
                raise ArchiveIntakeCapacityError("archive JSON scalar-byte budget exceeded")
        if nodes > budget.json_nodes:
            raise ArchiveIntakeCapacityError("archive JSON node budget exceeded")
    return nodes


def _strict_json(
    encoded: bytes, budget: ArchiveIntakeBudget, remaining_nodes: int | None = None
) -> tuple[dict[str, object], int]:
    nodes = _json_resource_scan(encoded, budget)
    if remaining_nodes is not None and nodes > remaining_nodes:
        raise ArchiveIntakeCapacityError("archive total JSON node budget exceeded")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ArchiveIntakeError("archive JSON contains duplicate keys")
            value[key] = item
        return value

    def finite_float(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ArchiveIntakeError("archive JSON contains a nonfinite number")
        return value

    def reject_constant(_text: str) -> None:
        raise ArchiveIntakeError("archive JSON contains a nonfinite number")

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_float=finite_float,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ArchiveIntakeError("archive member must be bounded UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ArchiveIntakeError("archive JSON envelope must be an object")
    return payload, nodes


def decode_personal_archive_json(encoded: bytes, budget: ArchiveIntakeBudget) -> dict[str, object]:
    """Decode one bounded, finite, duplicate-free request options object."""
    return _strict_json(encoded, budget)[0]


_LOGICAL_MEMBERS = frozenset({"manifest.json", "metadata.json", "graph.json", "content.json"})
_CONTENT_TABLES = frozenset(
    {
        "crawl_sources",
        "crawled_documents",
        "document_chunks",
        "entity",
        "raw_captures",
        "eval_attempts",
        "eval_consolidations",
        "api_idempotency_records",
        "source_imports",
        "content_changefeed_cursors",
        "derived_from",
        "chunk_of",
        "supersedes",
        "extracted_into",
        "system_settings",
        "telemetry_rollups",
        "backup_settings",
        "backups",
        "dream_source_checkpoints",
        "dream_source_cursors",
        "memory_validation_executions",
        "memory_validation_attempts",
    }
)


def _declared_count(payload: dict[str, object], name: str, actual: int) -> None:
    if name in payload and (type(payload[name]) is not int or payload[name] != actual):
        raise ArchiveIntakeError("archive declared counts must match logical rows")


def _logical_envelopes(
    files: dict[str, bytes], payloads: dict[str, dict[str, object]]
) -> tuple[dict[str, object] | None, dict[str, object] | None, int]:
    graph, content = payloads.get("graph.json"), payloads.get("content.json")
    rows = 0
    if graph is not None:
        allowed = {
            "version",
            "created_at",
            "organization_id",
            "entity_count",
            "relationship_count",
            "episode_count",
            "mention_count",
            "entities",
            "relationships",
            "episodes",
            "mentions",
            "source_integrity",
            "lineage_validation",
            "metadata",
        }
        if (
            graph.keys() - allowed
            or not isinstance(graph.get("version"), str)
            or graph.get("version") not in {"2.0", "3.0"}
        ):
            raise ArchiveIntakeError("unsupported graph archive contract")
        for field, count in (
            ("entities", "entity_count"),
            ("relationships", "relationship_count"),
            ("episodes", "episode_count"),
            ("mentions", "mention_count"),
        ):
            collection = graph.get(field, [])
            if not isinstance(collection, list) or any(
                not isinstance(row, dict) for row in collection
            ):
                raise ArchiveIntakeError("graph archive rows must be typed objects")
            _declared_count(graph, count, len(collection))
            rows += len(collection)
    if content is not None:
        allowed = {
            "version",
            "created_at",
            "organization_id",
            "source_integrity",
            "validation_receipts",
            "lineage_validation",
            "tables",
            "row_counts",
            "total_rows",
        }
        if (
            content.keys() - allowed
            or not isinstance(content.get("version"), str)
            or content.get("version")
            not in {
                "1.0",
                "2.0",
                "2.1",
                "2.2",
                "2.3",
            }
        ):
            raise ArchiveIntakeError("unsupported content archive contract")
        tables, counts = content.get("tables"), content.get("row_counts", {})
        if not isinstance(tables, dict) or tables.keys() - _CONTENT_TABLES:
            raise ArchiveIntakeError("unsupported content archive table kind")
        if not isinstance(counts, dict) or counts.keys() - tables.keys():
            raise ArchiveIntakeError("content row counts refer to unknown tables")
        content_rows = 0
        for name, collection in tables.items():
            if not isinstance(collection, list) or any(
                not isinstance(row, dict) for row in collection
            ):
                raise ArchiveIntakeError("content archive rows must be typed objects")
            _declared_count(counts, name, len(collection))
            content_rows += len(collection)
        _declared_count(content, "total_rows", content_rows)
        rows += content_rows
    # Foreign ledgers and validation receipts also consume row capacity even
    # though they cannot become active source evidence in a personal import.
    for payload in (graph, content):
        if payload is None:
            continue
        section = payload.get("source_integrity")
        if isinstance(section, dict):
            for name in ("source_rows", "source_states", "derivations"):
                collection = section.get(name)
                if not isinstance(collection, list):
                    raise ArchiveIntakeError("archive source collections must be arrays")
                rows += len(collection)
        receipts = payload.get("validation_receipts")
        if receipts is not None:
            if not isinstance(receipts, dict):
                raise ArchiveIntakeError("archive validation receipts must be an object")
            collection = receipts.get("executions")
            if isinstance(collection, list):
                rows += len(collection)
    if graph is None and content is None:
        raise ArchiveIntakeError("personal archive has no logical payload")
    if set(files) - {"graph.json", "content.json"}:
        raise ArchiveIntakeError("unsupported personal archive member")
    return graph, content, rows


def _guard_embedded_json(
    payloads: dict[str, dict[str, object]], budget: ArchiveIntakeBudget, nodes: int
) -> int:
    """Bound serialized carriers before existing provenance/receipt decoders."""
    graph, content = payloads.get("graph.json"), payloads.get("content.json")
    records = []
    for payload in (graph, content):
        if payload is None:
            continue
        if payload is graph:
            for name in ("entities", "relationships"):
                collection = payload.get(name, [])
                if isinstance(collection, list):
                    records.extend(collection)
        else:
            tables = payload.get("tables")
            if isinstance(tables, dict):
                for collection in tables.values():
                    records.extend(collection)
        section = payload.get("source_integrity")
        if isinstance(section, dict):
            for row in section.get("source_rows", []):
                if isinstance(row, dict) and isinstance(row.get("record"), dict):
                    records.append(row["record"])
            for field in ("source_states", "derivations"):
                collection = section.get(field, [])
                if isinstance(collection, list):
                    records.extend(collection)

    for row in records:
        if not isinstance(row, dict):
            continue
        carriers = [
            value
            for key, value in row.items()
            if isinstance(value, str)
            and key
            in {"request_json", "result_json", "usage_json", "metadata", "validation_binding_json"}
        ]
        attributes = row.get("attributes")
        if isinstance(attributes, dict) and isinstance(attributes.get("metadata"), str):
            carriers.append(attributes["metadata"])
        for value in carriers:
            _, used = _strict_json(value.encode("utf-8"), budget, budget.json_nodes - nodes)
            nodes += used

    if content is not None:
        section, tables = content.get("validation_receipts"), content.get("tables")
        if isinstance(section, dict) and isinstance(tables, dict):
            executions = tables.get("memory_validation_executions", [])
            keys = {
                row.get("uuid"): row.get("recovery_key")
                for row in executions
                if isinstance(row, dict)
            }
            entries = section.get("executions", [])
            if not isinstance(entries, list):
                raise ArchiveIntakeError("archive receipt inventory must be an array")
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("status") != "journal":
                    continue
                encoded, key = entry.get("ciphertext"), keys.get(entry.get("execution_id"))
                if not isinstance(encoded, str) or not isinstance(key, str):
                    raise ArchiveIntakeError("archive receipt envelope is malformed")
                try:
                    plaintext = Fernet(key.encode("ascii")).decrypt(
                        base64.b64decode(encoded, validate=True)
                    )
                except (InvalidToken, ValueError, UnicodeError) as exc:
                    raise ArchiveIntakeError("archive receipt envelope is invalid") from exc
                # Foreign recovery keys authenticate bytes only against their
                # declared envelope, never against destination authority.
                _, used = _strict_json(plaintext, budget, budget.json_nodes - nodes)
                nodes += used
    return nodes


def _read_exact(stream: _MeasuredInflation, amount: int) -> bytes:
    chunks = []
    remaining = amount
    while remaining:
        chunk = stream.read(min(remaining, 65536))
        if not chunk:
            raise ArchiveIntakeError("archive member is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _check_pax(encoded: bytes) -> None:
    # Flat logical names require no path/size overrides or sparse extensions.
    # Ordinary tar writers add timestamp and ownership PAX headers.
    offset = 0
    seen = set()
    allowed = {b"mtime", b"atime", b"ctime", b"uid", b"gid", b"uname", b"gname"}
    while offset < len(encoded):
        separator = encoded.find(b" ", offset, offset + 21)
        if separator < 0 or not encoded[offset:separator].isdigit():
            raise ArchiveIntakeError("invalid archive PAX framing")
        length = int(encoded[offset:separator])
        end = offset + length
        if length < 5 or end > len(encoded) or encoded[end - 1 : end] != b"\n":
            raise ArchiveIntakeError("invalid archive PAX framing")
        key, equals, _value = encoded[separator + 1 : end - 1].partition(b"=")
        if equals != b"=" or key not in allowed or key in seen:
            raise ArchiveIntakeError("unsupported archive PAX override")
        seen.add(key)
        offset = end


def _read_members(
    inflated: _MeasuredInflation, budget: ArchiveIntakeBudget
) -> tuple[dict[str, bytes], dict[str, dict[str, object]], int, int]:
    members: dict[str, bytes] = {}
    payloads: dict[str, dict[str, object]] = {}
    nodes = headers = 0
    pending_pax = False
    while True:
        header = _read_exact(inflated, tarfile.BLOCKSIZE)
        if not any(header):
            if pending_pax or any(_read_exact(inflated, tarfile.BLOCKSIZE)):
                raise ArchiveIntakeError("archive tar terminator is invalid")
            while trailing := inflated.read(65536):
                if any(trailing):
                    raise ArchiveIntakeError("archive contains trailing non-tar data")
            return members, payloads, nodes, headers
        headers += 1
        if headers > budget.members:
            raise ArchiveIntakeCapacityError("archive member resource budget exceeded")
        info = tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="strict")
        if info.size < 0 or info.size > budget.member_bytes:
            raise ArchiveIntakeCapacityError("archive member resource budget exceeded")
        if info.type == tarfile.XHDTYPE:
            if pending_pax:
                raise ArchiveIntakeError("archive contains stacked PAX headers")
            encoded = _read_exact(inflated, info.size)
            _check_pax(encoded)
            pending_pax = True
        else:
            if (
                info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
                or info.name not in _LOGICAL_MEMBERS
                or info.name in members
            ):
                raise ArchiveIntakeError("archive contains an unsafe or unsupported member")
            encoded = _read_exact(inflated, info.size)
            remaining = budget.json_nodes - nodes
            if remaining <= 0:
                raise ArchiveIntakeCapacityError("archive total JSON node budget exceeded")
            payload, member_nodes = _strict_json(encoded, budget, remaining)
            nodes += member_nodes
            members[info.name], payloads[info.name] = encoded, payload
            pending_pax = False
        padding = (-info.size) % tarfile.BLOCKSIZE
        if padding and any(_read_exact(inflated, padding)):
            raise ArchiveIntakeError("archive member padding is invalid")


def _personal_manifest(payload: dict[str, object]) -> ArchiveManifest:
    required = {"version", "created_at", "organization_id", "source_store", "files"}
    if (
        not required <= payload.keys()
        or payload.keys() - required - {"metadata"}
        or payload["version"] != "1.0"
        or any(
            not isinstance(value := payload[key], str) or not value.strip()
            for key in ("created_at", "organization_id", "source_store")
        )
        or not isinstance(payload.get("metadata", {}), dict)
    ):
        raise ArchiveIntakeError("unsupported typed archive manifest")
    created = payload["created_at"]
    assert isinstance(created, str)
    try:
        if datetime.fromisoformat(created).tzinfo is None:
            raise ValueError("archive time has no timezone")
        organization, store = payload["organization_id"], payload["source_store"]
        assert isinstance(organization, str) and isinstance(store, str)
        ArchiveSourceOrigin(organization_id=organization, source_store=store)
    except ValueError as exc:
        raise ArchiveIntakeError("archive manifest origin or timestamp is invalid") from exc
    files = payload["files"]
    if not isinstance(files, dict) or not files or files.keys() - {"graph.json", "content.json"}:
        raise ArchiveIntakeError("unsupported archive logical member inventory")
    for name, entry in files.items():
        if (
            not isinstance(entry, dict)
            or not {"path", "sha256", "size_bytes"} <= entry.keys()
            or entry.keys() - {"path", "sha256", "size_bytes", "kind", "metadata"}
            or entry["path"] != name
            or type(entry["size_bytes"]) is not int
            or entry["size_bytes"] < 0
            or not isinstance(entry["sha256"], str)
            or len(entry["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in entry["sha256"])
            or not isinstance(entry.get("metadata", {}), dict)
        ):
            raise ArchiveIntakeError("unsupported typed archive member inventory")
        # The published generic builder defaults to 'other'. The logical name
        # still fixes its interpretation; explicit kinds must match that name.
        kind = entry.get("kind", "other")
        if kind != "other" and kind != name.removesuffix(".json"):
            raise ArchiveIntakeError("unsupported archive logical member kind")
    return ArchiveManifest.from_dict(payload)


def parse_personal_archive(source: Path, budget: ArchiveIntakeBudget) -> ParsedPersonalArchive:
    """Validate and stage only inert logical members from an owned upload spool."""
    digest = hashlib.sha256()
    compressed = 0
    with source.open("rb") as spool:
        for chunk in iter(lambda: spool.read(65536), b""):
            compressed += len(chunk)
            if compressed > budget.compressed_bytes:
                raise ArchiveIntakeCapacityError("archive compressed-byte budget exceeded")
            digest.update(chunk)
    try:
        with source.open("rb") as spool, gzip.GzipFile(fileobj=spool, mode="rb") as gz:
            inflated = _MeasuredInflation(gz, budget.inflated_bytes)
            members, payloads, nodes, headers = _read_members(inflated, budget)
    except (gzip.BadGzipFile, EOFError, tarfile.TarError, OSError, UnicodeError) as exc:
        raise ArchiveIntakeError("archive is truncated or invalid") from exc
    envelope = set(members) & {"manifest.json", "metadata.json"}
    if len(envelope) != 1:
        raise ArchiveIntakeError("archive requires exactly one supported envelope")
    files = {name: data for name, data in members.items() if name not in envelope}
    graph, content, rows = _logical_envelopes(files, payloads)
    if rows > budget.parsed_rows:
        raise ArchiveIntakeCapacityError("archive parsed-row budget exceeded")
    nodes = _guard_embedded_json(payloads, budget, nodes)
    if "manifest.json" in members:
        archive = LoadedArchive(source, _personal_manifest(payloads["manifest.json"]), files)
    else:
        try:
            archive = load_backup_archive(source, dict(members))
        except (ValueError, TypeError, KeyError) as exc:
            raise ArchiveIntakeError("unsupported archive backup envelope") from exc
    for payload in (graph, content):
        if payload is not None:
            declared_org = payload.get("organization_id")
            if declared_org is not None and declared_org != archive.manifest.organization_id:
                raise ArchiveIntakeError("archive logical organization differs from origin")
    try:
        errors = validate_archive(archive)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ArchiveIntakeError("archive logical integrity validation failed") from exc
    if errors:
        raise ArchiveIntakeError("archive logical integrity validation failed")
    origin = ArchiveSourceOrigin(
        organization_id=archive.manifest.organization_id, source_store=archive.manifest.source_store
    )
    try:
        graph, content, _lineage = seal_archive_lineage(graph, content)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ArchiveIntakeError("archive source protection validation failed") from exc
    inventory = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        for name, data in sorted(members.items())
    }
    # All allowed member names and base64 values are ASCII. Compute exact
    # canonical JSON size before allocating any encoded staging expansion.
    staging_bytes = (
        2
        + max(0, len(members) - 1)
        + sum(
            len(canonical_json(name)) + 3 + 4 * ((len(data) + 2) // 3)
            for name, data in members.items()
        )
    )
    if staging_bytes > budget.encoded_artifact_bytes:
        raise ArchiveIntakeCapacityError("archive encoded-artifact budget exceeded")
    staged = canonical_json(
        {name: base64.b64encode(data).decode("ascii") for name, data in sorted(members.items())}
    )
    if len(staged) != staging_bytes:
        raise ArchiveIntakeError("archive staging serialization differs from measured size")
    sizes = {
        "compressed_bytes": compressed,
        "inflated_bytes": inflated.bytes_read,
        "logical_members": len(members),
        "physical_tar_headers": headers,
        "json_nodes": nodes,
        "parsed_rows": rows,
        "encoded_artifact_bytes": staging_bytes,
        "resource_budget": asdict(budget),
    }
    return ParsedPersonalArchive(
        archive=archive,
        origin=origin,
        archive_sha256=digest.hexdigest(),
        artifact_sha256=archive_digest("sibyl-archive-artifact-v1", inventory),
        member_inventory_json=canonical_json(inventory),
        staged_payload_json=staged,
        measured_sizes_json=canonical_json(sizes),
        graph=graph,
        content=content,
    )
