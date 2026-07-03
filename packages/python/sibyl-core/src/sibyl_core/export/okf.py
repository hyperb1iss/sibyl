"""OKF v0.1 projection for Sibyl graph archives."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sibyl_core.migrate.archive import LoadedArchive, graph_payload_from_archive

OKF_VERSION = "0.1"
_ARRAY_KEYS = ("entities", "relationships", "episodes", "mentions")
_RESERVED_FILENAMES = frozenset({"index.md", "log.md"})


@dataclass(frozen=True)
class OkfBundle:
    """Deterministic OKF file tree."""

    files: dict[str, str]


def build_okf_bundle_from_archive(archive: LoadedArchive) -> OkfBundle:
    graph_payload = graph_payload_from_archive(archive)
    if graph_payload is None:
        msg = "Archive is missing graph.json"
        raise ValueError(msg)
    return build_okf_bundle_from_graph_payload(
        graph_payload,
        created_at=archive.manifest.created_at,
    )


def build_okf_bundle_from_graph_payload(
    graph_payload: dict[str, Any],
    *,
    created_at: str | None = None,
) -> OkfBundle:
    concept_paths = _concept_paths(graph_payload)
    files: dict[str, str] = {
        "index.md": _index_document(graph_payload, concept_paths),
        "log.md": _log_document(graph_payload, created_at=created_at),
        "sibyl-graph.md": _concept_document(
            frontmatter={
                "type": "Sibyl Graph Archive",
                "title": "Sibyl graph archive",
                "description": "Root metadata for a Sibyl OKF graph projection.",
                "timestamp": created_at or _optional_str(graph_payload.get("created_at")),
                "okf_version": OKF_VERSION,
                "sibyl_kind": "graph",
                "sibyl_id": _optional_str(graph_payload.get("organization_id")) or "graph",
                "sibyl_payload": {
                    key: value for key, value in graph_payload.items() if key not in _ARRAY_KEYS
                },
            },
            body="# Sibyl Graph Archive\n\nThis concept anchors the Sibyl OKF projection.\n",
        ),
    }

    outgoing = _outgoing_relationships(graph_payload, concept_paths)
    for kind, records in _iter_graph_records(graph_payload):
        for index, record in enumerate(records):
            path = concept_paths[_record_key(kind, record, index)]
            files[path] = _record_document(
                kind=kind,
                record=record,
                index=index,
                path=path,
                concept_paths=concept_paths,
                outgoing=outgoing,
            )
    return OkfBundle(files={path: files[path] for path in sorted(files)})


def write_okf_bundle(bundle: OkfBundle, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, content in bundle.files.items():
        target = output_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def reconstruct_graph_payload_from_okf_bundle(bundle_dir: Path) -> dict[str, Any]:
    root_payload: dict[str, Any] | None = None
    records: dict[str, list[tuple[int, dict[str, Any]]]] = {
        "entity": [],
        "relationship": [],
        "episode": [],
        "mention": [],
    }
    for path in sorted(bundle_dir.rglob("*.md")):
        if path.name in _RESERVED_FILENAMES:
            continue
        frontmatter = _parse_json_frontmatter(path.read_text(encoding="utf-8"))
        kind = _optional_str(frontmatter.get("sibyl_kind"))
        payload = frontmatter.get("sibyl_payload")
        if not isinstance(payload, dict):
            continue
        if kind == "graph":
            root_payload = dict(payload)
            continue
        if kind in records:
            records[kind].append((int(frontmatter.get("sibyl_order") or 0), payload))

    if root_payload is None:
        msg = "OKF bundle is missing sibyl graph metadata"
        raise ValueError(msg)

    graph_payload = dict(root_payload)
    graph_payload["entities"] = _ordered_payloads(records["entity"])
    graph_payload["relationships"] = _ordered_payloads(records["relationship"])
    graph_payload["episodes"] = _ordered_payloads(records["episode"])
    graph_payload["mentions"] = _ordered_payloads(records["mention"])
    return graph_payload


def validate_okf_bundle(bundle_dir: Path) -> list[str]:
    errors: list[str] = []
    markdown_files = sorted(bundle_dir.rglob("*.md"))
    if not markdown_files:
        return ["OKF bundle does not contain markdown files"]

    graph_seen = False
    for path in markdown_files:
        relative = path.relative_to(bundle_dir).as_posix()
        if path.name in _RESERVED_FILENAMES:
            continue
        try:
            frontmatter = _parse_json_frontmatter(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"{relative}: {exc}")
            continue
        if not _optional_str(frontmatter.get("type")):
            errors.append(f"{relative}: missing required OKF type")
        if frontmatter.get("sibyl_kind") == "graph":
            graph_seen = True

    if not graph_seen:
        errors.append("OKF bundle is missing sibyl graph metadata")
    if not errors:
        try:
            reconstruct_graph_payload_from_okf_bundle(bundle_dir)
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def _iter_graph_records(
    graph_payload: dict[str, Any],
) -> tuple[tuple[str, list[dict[str, Any]]], ...]:
    return (
        ("entity", _record_list(graph_payload.get("entities"))),
        ("relationship", _record_list(graph_payload.get("relationships"))),
        ("episode", _record_list(graph_payload.get("episodes"))),
        ("mention", _record_list(graph_payload.get("mentions"))),
    )


def _record_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            records.append({str(key): item_value for key, item_value in item.items()})
    return records


def _concept_paths(graph_payload: dict[str, Any]) -> dict[tuple[str, int], str]:
    paths: dict[tuple[str, int], str] = {}
    used: set[str] = set()
    directories = {
        "entity": "entities",
        "relationship": "relationships",
        "episode": "episodes",
        "mention": "mentions",
    }
    for kind, records in _iter_graph_records(graph_payload):
        for index, record in enumerate(records):
            record_id = _record_identifier(kind, record) or f"{kind}-{index + 1}"
            slug = _safe_slug(record_id)
            path = f"{directories[kind]}/{slug}.md"
            if path in used:
                path = f"{directories[kind]}/{slug}-{index + 1}.md"
            used.add(path)
            paths[_record_key(kind, record, index)] = path
    return paths


def _record_key(kind: str, record: dict[str, Any], index: int) -> tuple[str, int]:
    del record
    return kind, index


def _record_document(
    *,
    kind: str,
    record: dict[str, Any],
    index: int,
    path: str,
    concept_paths: dict[tuple[str, int], str],
    outgoing: dict[str, list[dict[str, Any]]],
) -> str:
    record_id = _record_identifier(kind, record) or f"{kind}-{index + 1}"
    title = _record_title(kind, record, record_id)
    frontmatter: dict[str, Any] = {
        "type": _record_okf_type(kind, record),
        "title": title,
        "description": _record_description(kind, record),
        "timestamp": _record_timestamp(record),
        "okf_version": OKF_VERSION,
        "sibyl_kind": kind,
        "sibyl_id": record_id,
        "sibyl_order": index,
        "sibyl_path": path,
        "sibyl_payload": record,
    }
    if kind == "entity":
        frontmatter["sibyl_entity_type"] = _optional_str(record.get("entity_type"))
        if links := outgoing.get(record_id):
            frontmatter["edges"] = links
    if kind == "relationship":
        frontmatter["edges"] = [_relationship_edge(record, concept_paths)]
    body = _record_body(kind=kind, record=record, record_id=record_id, outgoing=outgoing)
    return _concept_document(frontmatter=frontmatter, body=body)


def _concept_document(*, frontmatter: dict[str, Any], body: str) -> str:
    return f"---\n{json.dumps(frontmatter, indent=2, sort_keys=True, default=str)}\n---\n\n{body}"


def _record_body(
    *,
    kind: str,
    record: dict[str, Any],
    record_id: str,
    outgoing: dict[str, list[dict[str, Any]]],
) -> str:
    title = _record_title(kind, record, record_id)
    body = [f"# {title}", ""]
    content = _optional_str(record.get("content") or record.get("description"))
    if content:
        body.extend([content, ""])
    if kind == "entity" and (links := outgoing.get(record_id)):
        body.extend(["## Relationships", ""])
        for link in links:
            label = link.get("type") or "related"
            target = link.get("target") or "unknown"
            target_path = link.get("target_path")
            if target_path:
                body.append(f"- {label}: [{target}]({target_path})")
            else:
                body.append(f"- {label}: {target}")
        body.append("")
    if kind == "relationship":
        source = _relationship_source(record) or "unknown"
        target = _relationship_target(record) or "unknown"
        body.extend(["## Relationship", "", f"- Source: `{source}`", f"- Target: `{target}`", ""])
    body.append("## Sibyl Metadata")
    body.append("")
    body.append(f"- Sibyl ID: `{record_id}`")
    body.append(f"- Sibyl kind: `{kind}`")
    return "\n".join(body) + "\n"


def _outgoing_relationships(
    graph_payload: dict[str, Any],
    concept_paths: dict[tuple[str, int], str],
) -> dict[str, list[dict[str, Any]]]:
    entity_paths = _paths_by_identifier("entity", graph_payload, concept_paths)
    outgoing: dict[str, list[dict[str, Any]]] = {}
    relationships = _record_list(graph_payload.get("relationships"))
    for relationship in relationships:
        source = _relationship_source(relationship)
        target = _relationship_target(relationship)
        if not source or not target:
            continue
        outgoing.setdefault(source, []).append(
            {
                "type": _relationship_type(relationship),
                "target": target,
                "target_path": f"/{entity_paths[target]}" if target in entity_paths else None,
                "weight": relationship.get("weight"),
            }
        )
    return outgoing


def _paths_by_identifier(
    kind: str,
    graph_payload: dict[str, Any],
    concept_paths: dict[tuple[str, int], str],
) -> dict[str, str]:
    paths: dict[str, str] = {}
    records_by_kind = dict(_iter_graph_records(graph_payload))
    for index, record in enumerate(records_by_kind.get(kind, [])):
        if record_id := _record_identifier(kind, record):
            paths[record_id] = concept_paths[_record_key(kind, record, index)]
    return paths


def _relationship_edge(
    record: dict[str, Any],
    concept_paths: dict[tuple[str, int], str],
) -> dict[str, Any]:
    del concept_paths
    return {
        "type": _relationship_type(record),
        "source": _relationship_source(record),
        "target": _relationship_target(record),
        "weight": record.get("weight"),
    }


def _record_identifier(kind: str, record: dict[str, Any]) -> str | None:
    keys = ("id", "uuid") if kind != "relationship" else ("id", "uuid", "name")
    for key in keys:
        if value := _optional_str(record.get(key)):
            return value
    if kind == "relationship":
        source = _relationship_source(record)
        target = _relationship_target(record)
        rel_type = _relationship_type(record)
        if source and target:
            return f"{source}-{rel_type}-{target}"
    return None


def _record_title(kind: str, record: dict[str, Any], record_id: str) -> str:
    if title := _optional_str(record.get("name") or record.get("title")):
        return title
    return f"{kind.title()} {record_id}"


def _record_description(kind: str, record: dict[str, Any]) -> str:
    if description := _optional_str(record.get("description")):
        return description
    entity_type = _optional_str(record.get("entity_type"))
    if kind == "entity" and entity_type:
        return f"Sibyl {entity_type} entity."
    return f"Sibyl {kind} projection."


def _record_okf_type(kind: str, record: dict[str, Any]) -> str:
    if kind == "entity":
        entity_type = _optional_str(record.get("entity_type"))
        return f"Sibyl {entity_type.title()} Entity" if entity_type else "Sibyl Entity"
    return f"Sibyl {kind.title()}"


def _record_timestamp(record: dict[str, Any]) -> str | None:
    return _optional_str(
        record.get("updated_at") or record.get("created_at") or record.get("valid_at")
    )


def _relationship_source(record: dict[str, Any]) -> str | None:
    return _optional_str(record.get("source_id") or record.get("source_node_uuid"))


def _relationship_target(record: dict[str, Any]) -> str | None:
    return _optional_str(record.get("target_id") or record.get("target_node_uuid"))


def _relationship_type(record: dict[str, Any]) -> str:
    return (
        _optional_str(
            record.get("relationship_type") or record.get("rel_type") or record.get("name")
        )
        or "related"
    )


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip(".-").lower()
    return slug or "concept"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _index_document(
    graph_payload: dict[str, Any],
    concept_paths: dict[tuple[str, int], str],
) -> str:
    lines = [
        "# Sibyl OKF Export",
        "",
        "Portable OKF v0.1 projection of a Sibyl graph archive.",
        "",
        "## Concepts",
        "",
        "- [Sibyl graph archive](/sibyl-graph.md) - root archive metadata",
    ]
    labels = {
        "entity": "Entities",
        "relationship": "Relationships",
        "episode": "Episodes",
        "mention": "Mentions",
    }
    for kind, records in _iter_graph_records(graph_payload):
        if not records:
            continue
        lines.extend(["", f"### {labels[kind]}", ""])
        for index, record in enumerate(records):
            record_id = _record_identifier(kind, record) or f"{kind}-{index + 1}"
            title = _record_title(kind, record, record_id)
            path = concept_paths[_record_key(kind, record, index)]
            lines.append(f"- [{title}](/{path}) - `{record_id}`")
    return "\n".join(lines) + "\n"


def _log_document(graph_payload: dict[str, Any], *, created_at: str | None) -> str:
    date = (created_at or _optional_str(graph_payload.get("created_at")) or "1970-01-01")[:10]
    return (
        "# Sibyl OKF Export Log\n\n"
        f"## {date}\n"
        "* **Export**: Generated Sibyl OKF projection from graph archive.\n"
    )


def _parse_json_frontmatter(content: str) -> dict[str, Any]:
    if not content.startswith("---\n"):
        msg = "missing YAML frontmatter delimiter"
        raise ValueError(msg)
    _, frontmatter, _body = content.split("---", 2)
    try:
        payload = json.loads(frontmatter)
    except json.JSONDecodeError as exc:
        msg = f"frontmatter is not JSON-compatible YAML: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = "frontmatter must be an object"
        raise ValueError(msg)
    return payload


def _ordered_payloads(records: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [payload for _order, payload in sorted(records, key=lambda item: item[0])]


__all__ = [
    "OKF_VERSION",
    "OkfBundle",
    "build_okf_bundle_from_archive",
    "build_okf_bundle_from_graph_payload",
    "reconstruct_graph_payload_from_okf_bundle",
    "validate_okf_bundle",
    "write_okf_bundle",
]
