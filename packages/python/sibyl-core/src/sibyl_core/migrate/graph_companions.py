"""Typed dates at the public graph companion archive boundary."""

from __future__ import annotations

from typing import Any

from sibyl_core.migrate.legacy_graph_archive import (
    episode_from_payload,
    episode_payload_from_node,
    mention_from_payload,
    mention_payload_from_edge,
    parse_backup_datetime,
)
from sibyl_core.migrate.source_integrity import decode_record, encode_record
from sibyl_core.models.entities import Relationship
from sibyl_core.services.graph_records import relationship_from_surreal_row

DATETIME_PATHS = "archive_datetime_paths"


def relationship_from_archive(payload: dict[str, Any]) -> Relationship:
    """Decode declared dates without interpreting ordinary metadata strings."""
    record = dict(payload)
    if DATETIME_PATHS in record:
        paths = record.pop(DATETIME_PATHS)
        record = decode_record({"record": record, "datetimes": paths})
    if record.get("created_at"):
        record["created_at"] = parse_backup_datetime(record["created_at"])
    return Relationship.model_validate(record)


def companion_payloads(
    snapshot: dict[str, Any], *, organization_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Serialize companions from the same precise native snapshot as sources."""
    auxiliary = snapshot["graph_auxiliary"]
    relationships = []
    for row in auxiliary["relates_to"]:
        relationship = relationship_from_surreal_row(
            {**row, "record_id": row["archive_record_key"]}
        )
        encoded = encode_record(relationship.model_dump(mode="python"))
        relationships.append({**encoded["record"], DATETIME_PATHS: encoded["datetimes"]})
    episodes = [
        episode_payload_from_node(
            episode_from_payload(row, organization_id=organization_id),
            organization_id=organization_id,
        )
        for row in auxiliary["episode"]
    ]
    records = {
        row["archive_record_key"]: row["uuid"]
        for row in [*snapshot["source_rows"], *auxiliary["episode"]]
    }
    mentions = [
        mention_payload_from_edge(
            mention_from_payload(
                {
                    **row,
                    "source_id": records[row["source_record_key"]],
                    "target_id": records[row["target_record_key"]],
                },
                organization_id=organization_id,
            ),
            organization_id=organization_id,
        )
        for row in auxiliary["mentions"]
    ]
    return relationships, episodes, mentions
