"""Materialized memory-as-files projection for ``.sibyl/memory/``.

The graph stays the governed source of truth; this module renders a curated,
greppable slice of it into a directory an agent can read with the server down.

Every function here is a pure transform over an explicit :class:`MemorySnapshot`.
Nothing consults the clock, the network, or the package version, because the
projection is meant to live in git: two runs over an unchanged graph must
produce byte-identical files or every session emits a noise diff.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from sibyl_core.export._slug import safe_slug

MEMORY_FILES_SCHEMA_VERSION = "sibyl-memory-files-v1"
MEMORY_FILES_OKF_VERSION = "0.1"

README_PATH = "README.md"
INDEX_PATH = "index.md"
RECENT_PATH = "recent.md"
TASKS_PATH = "tasks.md"
HANDBOOK_PATH = "handbook.md"
MANIFEST_PATH = "manifest.json"
NOTES_DIR = "notes"

RECENT_PREVIEW_CHARS = 320
NOTE_STEM_MAX_CHARS = 100
NOTE_ID_SUFFIX_MAX_CHARS = 64
FRONTMATTER_TEXT_CHARS = 200

# Usage counters, embedding bookkeeping, and Surreal record handles all mutate
# when memory is merely *read*, so a whitelist rather than a blacklist is what
# keeps a re-materialization byte-identical.
_TASK_METADATA_FIELDS = ("status", "priority", "complexity", "feature", "epic_id")


@dataclass(frozen=True)
class MemoryRecord:
    """One durable memory item, stripped of volatile bookkeeping."""

    id: str
    entity_type: str
    title: str
    body: str = ""
    updated_at: str | None = None
    project_id: str | None = None
    tags: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MemorySnapshot:
    """Everything the projection is allowed to see."""

    project_id: str
    project_name: str | None = None
    project_description: str | None = None
    notes: tuple[MemoryRecord, ...] = ()
    tasks: tuple[MemoryRecord, ...] = ()
    handbook: str | None = None


@dataclass(frozen=True)
class MemoryFilesBundle:
    """Deterministic ``.sibyl/memory/`` file tree, keyed by relative posix path."""

    files: Mapping[str, str] = field(default_factory=dict)

    @property
    def content_digest(self) -> str:
        return _digest_of_files(self.files)


def memory_record_from_entity(payload: Mapping[str, Any]) -> MemoryRecord:
    """Project a Sibyl entity envelope onto the whitelisted record shape."""
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}

    record_id = _text(payload.get("id")) or _text(metadata.get("id")) or ""
    entity_type = (
        _text(payload.get("type")) or _text(metadata.get("entity_type")) or "entity"
    ).lower()
    title = (
        _text(payload.get("name")) or _text(metadata.get("title")) or record_id or entity_type
    ).strip()

    # List responses truncate the top-level description; metadata carries the
    # full text, so the longer of the two is the honest body.
    candidates = [
        _text(metadata.get("content")),
        _text(metadata.get("description")),
        _text(payload.get("content")),
        _text(payload.get("description")),
    ]
    body = max((value for value in candidates if value), key=len, default="")

    attributes = tuple(
        (key, value)
        for key in _TASK_METADATA_FIELDS
        if (value := _text(metadata.get(key))) is not None
    )

    return MemoryRecord(
        id=record_id,
        entity_type=entity_type,
        title=title,
        body=body or "",
        updated_at=_text(metadata.get("updated_at")) or _text(payload.get("updated_at")),
        project_id=_text(metadata.get("project_id")) or _text(payload.get("project_id")),
        tags=_string_tuple(metadata.get("tags")),
        attributes=attributes,
    )


def build_memory_files_bundle(snapshot: MemorySnapshot) -> MemoryFilesBundle:
    """Render a snapshot into the ``.sibyl/memory/`` file tree."""
    notes = _canonical_records(snapshot.notes)
    tasks = _canonical_records(snapshot.tasks)
    note_paths = _note_paths(notes)

    files: dict[str, str] = {
        README_PATH: _readme_document(snapshot),
        INDEX_PATH: _index_document(snapshot, notes, tasks, note_paths),
        RECENT_PATH: _recent_document(snapshot, notes, note_paths),
        TASKS_PATH: _tasks_document(snapshot, tasks),
    }
    if snapshot.handbook is not None:
        files[HANDBOOK_PATH] = _handbook_document(snapshot, snapshot.handbook)
    for record in notes:
        files[note_paths[record.id]] = _note_document(snapshot, record)

    files[MANIFEST_PATH] = _manifest_document(snapshot, files, notes=notes, tasks=tasks)
    return MemoryFilesBundle(files={path: files[path] for path in sorted(files)})


def write_memory_files_bundle(
    bundle: MemoryFilesBundle,
    root: Path,
    *,
    read_only: bool = True,
    force: bool = False,
) -> tuple[str, ...]:
    """Write the bundle to ``root``, pruning what a previous export wrote.

    Returns the written paths. The previous manifest is the only authority on
    what Sibyl owns, so a file this exporter never wrote is never removed even
    when it sits inside ``notes/``. A populated directory that is not already an
    export is refused rather than overwritten: ``--output .`` in a repo root
    would otherwise eat that repo's README.
    """
    if root.exists() and not root.is_dir():
        msg = f"Memory files output path exists and is not a directory: {root}"
        raise FileExistsError(msg)

    previous = _previously_written(root)
    if not force and previous is None and root.is_dir() and any(root.iterdir()):
        msg = (
            f"Refusing to overwrite {root}: it is not empty and holds no Sibyl "
            f"{MANIFEST_PATH}. Pass force=True to write anyway."
        )
        raise FileExistsError(msg)

    root.mkdir(parents=True, exist_ok=True)
    for relative in sorted((previous or set()) - set(bundle.files)):
        stale = root / relative
        if stale.is_file():
            stale.chmod(0o644)
            stale.unlink()

    for relative_path, content in bundle.files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.chmod(0o644)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o444 if read_only else 0o644)

    return tuple(bundle.files)


def _previously_written(root: Path) -> set[str] | None:
    """Paths a previous export of this layout wrote, or None if there was none."""
    manifest_path = root / MANIFEST_PATH
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != (
        MEMORY_FILES_SCHEMA_VERSION
    ):
        return None
    entries = manifest.get("files")
    written = {MANIFEST_PATH}
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and (path := entry.get("path")):
            written.add(str(path))
    return written


def validate_memory_files_bundle(root: Path) -> list[str]:
    """Check a materialized directory against the layout contract."""
    errors: list[str] = []
    manifest_path = root / MANIFEST_PATH
    if not manifest_path.is_file():
        return [f"missing {MANIFEST_PATH}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{MANIFEST_PATH}: not valid JSON: {exc}"]
    if not isinstance(manifest, dict):
        return [f"{MANIFEST_PATH}: must be an object"]
    if manifest.get("schema_version") != MEMORY_FILES_SCHEMA_VERSION:
        errors.append(
            f"{MANIFEST_PATH}: unexpected schema_version {manifest.get('schema_version')}"
        )

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        return [*errors, f"{MANIFEST_PATH}: files must be a non-empty array"]

    listed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append(f"{MANIFEST_PATH}: file entries must be objects")
            continue
        relative = str(entry.get("path") or "")
        listed.add(relative)
        target = root / relative
        if not target.is_file():
            errors.append(f"{relative}: listed in the manifest but missing on disk")
            continue
        content = target.read_text(encoding="utf-8")
        if _digest_of_text(content) != entry.get("sha256"):
            errors.append(f"{relative}: content does not match the manifest digest")

    if INDEX_PATH not in listed:
        errors.append(f"{MANIFEST_PATH}: missing {INDEX_PATH}")

    for markdown in sorted(root.rglob("*.md")):
        relative = markdown.relative_to(root).as_posix()
        if relative not in listed:
            continue
        if not _frontmatter_type(markdown.read_text(encoding="utf-8")):
            errors.append(f"{relative}: missing required OKF frontmatter type")

    return errors


def _canonical_records(records: Iterable[MemoryRecord]) -> tuple[MemoryRecord, ...]:
    return tuple(sorted(records, key=lambda record: (record.id, record.title)))


def _note_paths(records: Sequence[MemoryRecord]) -> dict[str, str]:
    """Assign one stable path per record.

    A stem shared by several records disambiguates *every* one of them with its
    id, so which file a record lands in depends only on the set and never on the
    order it arrived in. Whoever sorts first does not get to keep the short name.
    """
    stems = {record.id: _note_stem(record) for record in records}
    collisions = {stem for stem in stems.values() if list(stems.values()).count(stem) > 1}
    return {
        record_id: f"{NOTES_DIR}/{stem}-{_id_suffix(record_id)}.md"
        if stem in collisions
        else f"{NOTES_DIR}/{stem}.md"
        for record_id, stem in stems.items()
    }


def _id_suffix(record_id: str) -> str:
    return safe_slug(record_id)[:NOTE_ID_SUFFIX_MAX_CHARS]


def _note_stem(record: MemoryRecord) -> str:
    """Build the filename stem, capped so a long title cannot exceed NAME_MAX.

    Memory titles are prose and run long; the id suffix and `.md` still have to
    fit after the cap, or the write aborts partway through the tree.
    """
    stem = f"{safe_slug(record.entity_type, fallback='entity')}-{safe_slug(record.title)}"
    return stem[:NOTE_STEM_MAX_CHARS].rstrip("-.") or safe_slug(record.id)


def _by_recency(records: Iterable[MemoryRecord]) -> list[MemoryRecord]:
    """Newest first, id ascending as the tiebreak so equal timestamps never flip."""
    by_id = sorted(records, key=lambda record: record.id)
    return sorted(by_id, key=lambda record: record.updated_at or "", reverse=True)


def _readme_document(snapshot: MemorySnapshot) -> str:
    body = "\n".join(
        [
            "# Sibyl memory",
            "",
            f"Materialized memory for **{_project_label(snapshot)}**"
            f" (`{snapshot.project_id}`), generated by `sibyl export memory`.",
            "",
            "Generated files. Edit the graph, not this directory: the next export",
            "overwrites everything here. Files are written read-only as a reminder.",
            "",
            "## Reading order",
            "",
            f"1. `{HANDBOOK_PATH}` - distilled project handbook, when one has been built.",
            f"2. `{INDEX_PATH}` - every materialized memory with its id and path.",
            f"3. `{RECENT_PATH}` - the most recently updated memories, newest first.",
            f"4. `{TASKS_PATH}` - open work with task ids.",
            f"5. `{NOTES_DIR}/` - one file per memory, full text.",
            "",
            "## Grepping",
            "",
            "```sh",
            f"rg 'surreal' .sibyl/memory/{NOTES_DIR}",
            f"rg -l 'type: Sibyl Decision' .sibyl/memory/{NOTES_DIR}",
            f"grep -n 'sibyl_id' .sibyl/memory/{INDEX_PATH}",
            "```",
            "",
            "## Citing",
            "",
            "Reading a file records nothing. When a memory shapes the work, cite it",
            "over the API so the usage signal stays honest:",
            "",
            "```sh",
            "sibyl cite <sibyl_id>",
            "```",
            "",
            f"`{MANIFEST_PATH}` carries a SHA-256 per file plus a `content_digest`",
            "over the whole projection, so a hook can detect staleness without a refetch.",
            "",
        ]
    )
    return _document(
        frontmatter=[
            ("type", "Sibyl Memory Export"),
            ("title", f"Sibyl memory for {_project_label(snapshot)}"),
            ("description", "Read-only projection of Sibyl memory for this repository."),
            ("okf_version", MEMORY_FILES_OKF_VERSION),
            ("sibyl_schema_version", MEMORY_FILES_SCHEMA_VERSION),
            ("sibyl_kind", "memory_export"),
            ("sibyl_project_id", snapshot.project_id),
        ],
        body=body,
    )


def _index_document(
    snapshot: MemorySnapshot,
    notes: Sequence[MemoryRecord],
    tasks: Sequence[MemoryRecord],
    note_paths: Mapping[str, str],
) -> str:
    lines = [
        f"# Memory index: {_project_label(snapshot)}",
        "",
        f"Project `{snapshot.project_id}` - {len(notes)} memories, {len(tasks)} open tasks.",
        "",
    ]
    if snapshot.handbook is not None:
        lines.extend([f"- Handbook: [{HANDBOOK_PATH}](./{HANDBOOK_PATH})", ""])
    lines.extend(
        [
            f"- Recent: [{RECENT_PATH}](./{RECENT_PATH})",
            f"- Tasks: [{TASKS_PATH}](./{TASKS_PATH})",
            "",
        ]
    )

    if not notes:
        lines.extend(["## Memories", "", "No memories materialized for this project yet.", ""])
        return _index_wrapper(snapshot, lines, notes, tasks)

    lines.extend(["## Memories", ""])
    for entity_type in sorted({record.entity_type for record in notes}):
        lines.extend([f"### {_type_label(entity_type)}", ""])
        for record in notes:
            if record.entity_type != entity_type:
                continue
            lines.append(f"- [{record.title}](./{note_paths[record.id]}) - `{record.id}`")
        lines.append("")
    return _index_wrapper(snapshot, lines, notes, tasks)


def _index_wrapper(
    snapshot: MemorySnapshot,
    lines: Sequence[str],
    notes: Sequence[MemoryRecord],
    tasks: Sequence[MemoryRecord],
) -> str:
    return _document(
        frontmatter=[
            ("type", "Sibyl Memory Index"),
            ("title", f"Memory index for {_project_label(snapshot)}"),
            ("description", "Inventory of every materialized Sibyl memory in this repository."),
            ("okf_version", MEMORY_FILES_OKF_VERSION),
            ("sibyl_schema_version", MEMORY_FILES_SCHEMA_VERSION),
            ("sibyl_kind", "memory_index"),
            ("sibyl_project_id", snapshot.project_id),
            ("sibyl_note_count", len(notes)),
            ("sibyl_task_count", len(tasks)),
        ],
        body="\n".join(lines),
    )


def _recent_document(
    snapshot: MemorySnapshot,
    notes: Sequence[MemoryRecord],
    note_paths: Mapping[str, str],
) -> str:
    ordered = _by_recency(notes)
    lines = [
        f"# Recent memory: {_project_label(snapshot)}",
        "",
        "Most recently updated memories first.",
        "",
    ]
    if not ordered:
        lines.extend(["No memories materialized for this project yet.", ""])
    for record in ordered:
        lines.append(f"## {record.title}")
        lines.append("")
        lines.append(f"- id: `{record.id}`")
        lines.append(f"- type: `{record.entity_type}`")
        if record.updated_at:
            lines.append(f"- updated: `{record.updated_at}`")
        lines.append(f"- detail: [{note_paths[record.id]}](./{note_paths[record.id]})")
        lines.append("")
        if preview := _preview(record.body):
            lines.extend([preview, ""])
    return _document(
        frontmatter=[
            ("type", "Sibyl Memory Digest"),
            ("title", f"Recent memory for {_project_label(snapshot)}"),
            ("description", "Recency-ordered digest of materialized Sibyl memory."),
            ("okf_version", MEMORY_FILES_OKF_VERSION),
            ("sibyl_schema_version", MEMORY_FILES_SCHEMA_VERSION),
            ("sibyl_kind", "memory_digest"),
            ("sibyl_project_id", snapshot.project_id),
            ("sibyl_note_count", len(ordered)),
        ],
        body="\n".join(lines),
    )


def _tasks_document(snapshot: MemorySnapshot, tasks: Sequence[MemoryRecord]) -> str:
    ordered = _by_recency(tasks)
    lines = [f"# Open work: {_project_label(snapshot)}", ""]
    if not ordered:
        lines.extend(["No open tasks materialized for this project.", ""])
    for record in ordered:
        attributes = dict(record.attributes)
        status = attributes.get("status", "unknown")
        priority = attributes.get("priority")
        suffix = f" - priority `{priority}`" if priority else ""
        lines.append(f"## [{status}] {record.title}")
        lines.append("")
        lines.append(f"- id: `{record.id}`{suffix}")
        if record.updated_at:
            lines.append(f"- updated: `{record.updated_at}`")
        if record.tags:
            lines.append(f"- tags: {', '.join(f'`{tag}`' for tag in record.tags)}")
        lines.append("")
        if preview := _preview(record.body):
            lines.extend([preview, ""])
    return _document(
        frontmatter=[
            ("type", "Sibyl Task Board"),
            ("title", f"Open work for {_project_label(snapshot)}"),
            ("description", "Materialized open Sibyl tasks with their ids."),
            ("okf_version", MEMORY_FILES_OKF_VERSION),
            ("sibyl_schema_version", MEMORY_FILES_SCHEMA_VERSION),
            ("sibyl_kind", "task_board"),
            ("sibyl_project_id", snapshot.project_id),
            ("sibyl_task_count", len(ordered)),
        ],
        body="\n".join(lines),
    )


def _handbook_document(snapshot: MemorySnapshot, handbook: str) -> str:
    body = handbook.strip()
    return _document(
        frontmatter=[
            ("type", "Sibyl Handbook"),
            ("title", f"Handbook for {_project_label(snapshot)}"),
            ("description", "Distilled project handbook maintained by the Sibyl dream cycle."),
            ("okf_version", MEMORY_FILES_OKF_VERSION),
            ("sibyl_schema_version", MEMORY_FILES_SCHEMA_VERSION),
            ("sibyl_kind", "handbook"),
            ("sibyl_project_id", snapshot.project_id),
        ],
        body=f"{body}\n" if body else "",
    )


def _note_document(snapshot: MemorySnapshot, record: MemoryRecord) -> str:
    frontmatter: list[tuple[str, Any]] = [
        ("type", f"Sibyl {_type_label(record.entity_type)}"),
        ("title", record.title),
        ("description", _flatten(record.body or record.title)),
        ("timestamp", record.updated_at),
        ("okf_version", MEMORY_FILES_OKF_VERSION),
        ("sibyl_schema_version", MEMORY_FILES_SCHEMA_VERSION),
        ("sibyl_kind", "memory_note"),
        ("sibyl_id", record.id),
        ("sibyl_entity_type", record.entity_type),
        ("sibyl_project_id", record.project_id or snapshot.project_id),
    ]
    if record.tags:
        frontmatter.append(("sibyl_tags", list(record.tags)))
    frontmatter.extend((f"sibyl_{key}", value) for key, value in record.attributes)

    lines = [f"# {record.title}", ""]
    if record.body:
        lines.extend([record.body.strip(), ""])
    lines.extend(
        [
            "## Provenance",
            "",
            f"- Sibyl id: `{record.id}`",
            f"- Entity type: `{record.entity_type}`",
            f"- Cite with: `sibyl cite {record.id}`",
            "",
        ]
    )
    return _document(frontmatter=frontmatter, body="\n".join(lines))


def _manifest_document(
    snapshot: MemorySnapshot,
    files: Mapping[str, str],
    *,
    notes: Sequence[MemoryRecord],
    tasks: Sequence[MemoryRecord],
) -> str:
    entries = [{"path": path, "sha256": _digest_of_text(files[path])} for path in sorted(files)]
    manifest = {
        "schema_version": MEMORY_FILES_SCHEMA_VERSION,
        "okf_version": MEMORY_FILES_OKF_VERSION,
        "project": {
            "id": snapshot.project_id,
            "name": snapshot.project_name,
        },
        "counts": {
            "notes": len(notes),
            "tasks": len(tasks),
            "files": len(entries) + 1,
        },
        "handbook": snapshot.handbook is not None,
        "content_digest": _digest_of_files(files),
        "files": entries,
    }
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def _digest_of_text(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _digest_of_files(files: Mapping[str, str]) -> str:
    digest = sha256()
    for path in sorted(files):
        if path == MANIFEST_PATH:
            continue
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[path].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _document(*, frontmatter: Sequence[tuple[str, Any]], body: str) -> str:
    return f"---\n{_frontmatter_block(frontmatter)}---\n\n{body.rstrip()}\n"


def _frontmatter_block(fields: Sequence[tuple[str, Any]]) -> str:
    lines: list[str] = []
    for key, value in fields:
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
                continue
            lines.append(f"{key}:")
            lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
            continue
        lines.append(f"{key}: {_yaml_scalar(value)}")
    return "".join(f"{line}\n" for line in lines)


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _quote(str(value))


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    escaped = "".join(char for char in escaped if char >= " " or char == "\\")
    return f'"{escaped}"'


def _frontmatter_type(content: str) -> str | None:
    if not content.startswith("---\n"):
        return None
    end = content.find("\n---\n", len("---\n"))
    if end == -1:
        return None
    for line in content[len("---\n") : end].splitlines():
        if line.startswith("type:"):
            return line[len("type:") :].strip().strip('"') or None
    return None


def _preview(body: str) -> str:
    flattened = _flatten(body, limit=RECENT_PREVIEW_CHARS)
    return f"> {flattened}" if flattened else ""


def _flatten(value: str, *, limit: int = FRONTMATTER_TEXT_CHARS) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "\u2026"


def _project_label(snapshot: MemorySnapshot) -> str:
    return snapshot.project_name or snapshot.project_id


def _type_label(entity_type: str) -> str:
    return entity_type.replace("_", " ").title()


def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(sorted({text for item in value if (text := _text(item)) is not None}))


__all__ = [
    "HANDBOOK_PATH",
    "INDEX_PATH",
    "MANIFEST_PATH",
    "MEMORY_FILES_OKF_VERSION",
    "MEMORY_FILES_SCHEMA_VERSION",
    "NOTES_DIR",
    "README_PATH",
    "RECENT_PATH",
    "TASKS_PATH",
    "MemoryFilesBundle",
    "MemoryRecord",
    "MemorySnapshot",
    "build_memory_files_bundle",
    "memory_record_from_entity",
    "validate_memory_files_bundle",
    "write_memory_files_bundle",
]
