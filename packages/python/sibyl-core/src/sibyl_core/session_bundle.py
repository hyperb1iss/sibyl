"""Shared helpers for packaging session context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SESSION_PREVIEW_CHARS = 180


def format_preview(content: str, max_chars: int = SESSION_PREVIEW_CHARS) -> str:
    preview = content.strip()
    if preview.startswith("[") and "] " in preview:
        preview = preview.split("] ", 1)[1]
    preview = " ".join(preview.split())
    if len(preview) <= max_chars:
        return preview

    cutoff = preview.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return preview[:cutoff].rstrip() + "…"


def _normalized_memory_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def memory_dedupe_keys(memory: Mapping[str, Any]) -> set[str]:
    keys: set[str] = set()
    memory_id = _normalized_memory_text(memory.get("id"))
    if memory_id:
        keys.add(f"id:{memory_id}")

    name = _normalized_memory_text(memory.get("name"))
    preview = _normalized_memory_text(memory.get("preview"))
    if name and preview:
        keys.add(f"summary:{name}:{preview[:120]}")
    return keys


def summarize_task(task: Mapping[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}

    return {
        "id": task.get("id", ""),
        "name": task.get("name", ""),
        "status": metadata.get("status", ""),
        "priority": metadata.get("priority", ""),
        "feature": metadata.get("feature"),
        "branch_name": metadata.get("branch_name"),
    }


def summarize_memory(entity: Mapping[str, Any]) -> dict[str, Any]:
    metadata = entity.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}

    return {
        "id": entity.get("id", ""),
        "name": entity.get("name", "Unknown"),
        "entity_type": entity.get("entity_type") or entity.get("type"),
        "source": entity.get("source"),
        "preview": format_preview(str(entity.get("content", ""))),
        "document_id": metadata.get("document_id"),
        "memory_scope": entity.get("memory_scope") or metadata.get("memory_scope"),
        "scope_key": entity.get("scope_key") or metadata.get("scope_key"),
    }


def summarize_raw_memory(memory: Mapping[str, Any]) -> dict[str, Any]:
    raw_id = str(memory.get("id", ""))
    memory_id = raw_id if raw_id.startswith("raw_memory:") else f"raw_memory:{raw_id}"
    return {
        "id": memory_id,
        "name": memory.get("title") or "Untitled raw memory",
        "entity_type": "raw_memory",
        "source": memory.get("source_id") or memory.get("capture_surface"),
        "preview": format_preview(str(memory.get("raw_content", ""))),
        "document_id": None,
        "memory_scope": memory.get("memory_scope"),
        "scope_key": memory.get("scope_key"),
    }


def derive_query(
    explicit_query: str | None,
    tasks: Sequence[Mapping[str, Any]],
    project_name: str | None,
) -> str | None:
    if explicit_query:
        query = explicit_query.strip()
        return query or None

    task_titles = [str(task.get("name", "")).strip() for task in tasks if task.get("name")]
    if task_titles:
        return " | ".join(task_titles[:2])[:140]

    if project_name:
        project = project_name.strip()
        return project or None

    return None


def remember_next(
    tasks: Sequence[Mapping[str, Any]],
    relevant_entities: Sequence[Mapping[str, Any]],
    has_project: bool,
) -> str:
    blocked = next((task for task in tasks if task.get("status") == "blocked"), None)
    if blocked:
        return f"Unblock {blocked.get('name', 'the blocked task')} before you pick up new work."

    doing = next((task for task in tasks if task.get("status") == "doing"), None)
    if doing:
        return (
            f"Continue {doing.get('name', 'your active task')} and capture anything non-obvious "
            "with `sibyl remember`."
        )

    if relevant_entities:
        return (
            f"Review {relevant_entities[0].get('name', 'the top memory')} before you dive back in."
        )

    if has_project:
        return "No active tasks yet. Start one or remember the next useful learning."

    return "Link this directory to a project so session context stays scoped."
