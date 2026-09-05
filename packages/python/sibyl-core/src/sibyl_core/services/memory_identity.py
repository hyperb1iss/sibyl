"""Versioned identities for immutable reflection evidence and assertions."""

from __future__ import annotations

import hashlib
import json

from sibyl_core.models.entities import Entity

IDENTITY_KEY = "reflection_identity"


def reflection_identity(entity: Entity) -> dict[str, object]:
    """Bind full evidence to its authoritative ownership and provenance.

    Title whitespace follows graph read normalization; content is byte-exact. Extraction
    UUIDs, timestamps and lifecycle decisions are deliberately not identities.
    Legacy title-derived IDs remain readable and are never rewritten here.
    """
    metadata = entity.metadata
    source_ids = {
        str(value)
        for key in ("raw_source_ids", "source_ids")
        for value in metadata.get(key, [])
        if value
    }
    if entity.source_file:
        source_ids.add(entity.source_file)
    identity = {
        "version": 2,
        "purpose": "source" if metadata.get("reflection_source") is True else "candidate",
        "kind": entity.entity_type.value,
        "title": entity.name.strip(),
        "content_sha256": hashlib.sha256(entity.content.encode("utf-8")).hexdigest(),
        "organization_id": entity.organization_id,
        "principal_id": metadata.get("principal_id"),
        "created_by": entity.created_by,
        "memory_scope": metadata.get("memory_scope"),
        "scope_key": metadata.get("scope_key"),
        "project_id": metadata.get("project_id"),
        "domain": metadata.get("category") or None,
        "primary_source_id": entity.source_file,
        "source_ids": sorted(source_ids),
        "review_capture_id": metadata.get("review_capture_id"),
        "imported_capture_id": metadata.get("imported_capture_id"),
    }
    # Surreal NONE removes object keys. Canonical absence must therefore be
    # identical before serialization and after reading the stored evidence.
    return {key: value for key, value in identity.items() if value is not None}


def reflection_entity_id(entity: Entity) -> str:
    payload = json.dumps(reflection_identity(entity), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{entity.entity_type.value}_v2_{digest}"


def verify_reflection_identity(expected: Entity, stored: Entity) -> None:
    """Do not trust a stored fingerprint without checking its actual fields."""
    identity = reflection_identity(expected)
    if (
        expected.id != stored.id
        or expected.content != stored.content
        or stored.metadata.get(IDENTITY_KEY) != identity
        or reflection_identity(stored) != identity
    ):
        raise ValueError("reflection identity conflict; existing evidence was not modified")
