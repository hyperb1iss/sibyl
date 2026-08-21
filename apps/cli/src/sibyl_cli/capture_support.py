"""Shared validation and write mechanics for CLI memory capture."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

import typer

from sibyl_cli import command_support
from sibyl_cli.client import SibylClientError
from sibyl_cli.common import warn
from sibyl_cli.project_refs import resolve_project_reference
from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
from sibyl_core.models.entities import EntityType
from sibyl_core.models.memory_scope import MemoryScope

CAPTURE_TITLE_CHARS = 72
ENTITY_TYPE_ALIASES = {
    "gotcha": EntityType.ERROR_PATTERN.value,
    "learning": EntityType.NOTE.value,
}
ENTITY_TYPE_VALUES = [entity_type.value for entity_type in EntityType]
ADVERTISED_ENTITY_TYPE_VALUES = (
    "episode",
    "decision",
    "procedure",
    "error_pattern",
    "rule",
    "plan",
    "idea",
    "claim",
    "artifact",
    "session",
    "note",
)
ADVERTISED_MEMORY_SCOPE_VALUES = ("private", "project", "team", "org")
MEMORY_BASIS_VALUES = ("observed", "inferred", "told", "assumed")
MEMORY_PROPOSAL_SCOPE_VALUES = ("team",)
ENTITY_TYPE_HELP = f"Memory kind: {', '.join(ADVERTISED_ENTITY_TYPE_VALUES)}"
MEMORY_SCOPE_HELP = f"Memory scope: {', '.join(ADVERTISED_MEMORY_SCOPE_VALUES)}"


def normalize_entity_type(value: str, *, option_name: str) -> str:
    normalized = value.strip().lower()
    if alias := ENTITY_TYPE_ALIASES.get(normalized):
        warn(f"{option_name}={normalized} is deprecated; using {alias}.")
        return alias
    if normalized in ENTITY_TYPE_VALUES:
        return normalized
    choices = ", ".join(ENTITY_TYPE_VALUES)
    raise typer.BadParameter(f"{value!r} is not one of: {choices}")


def normalize_add_type(value: str) -> str:
    return normalize_entity_type(value, option_name="--type")


def normalize_memory_kind(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_entity_type(value, option_name="--kind")


def normalize_legacy_memory_kind(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_entity_type(value, option_name="--type")


def normalize_memory_basis(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in MEMORY_BASIS_VALUES:
        return normalized
    choices = ", ".join(MEMORY_BASIS_VALUES)
    raise typer.BadParameter(f"{value!r} is not one of: {choices}")


def normalize_memory_proposal_scope(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in MEMORY_PROPOSAL_SCOPE_VALUES:
        return normalized
    choices = ", ".join(MEMORY_PROPOSAL_SCOPE_VALUES)
    raise typer.BadParameter(f"{value!r} is not one of: {choices}")


def normalize_memory_scope(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "org":
        return "organization"
    try:
        return MemoryScope(normalized).value
    except ValueError as exc:
        choices = ", ".join(ADVERTISED_MEMORY_SCOPE_VALUES)
        raise typer.BadParameter(f"{value!r} is not one of: {choices}") from exc


def looks_like_task_id(value: str) -> bool:
    candidate = value.strip()
    if candidate.startswith("task_"):
        return True
    try:
        UUID(candidate)
    except ValueError:
        return False
    return True


def derive_capture_title(content: str) -> str:
    """Create a compact default title for quick captures."""
    compact = re.sub(r"\s+", " ", content).strip()
    if not compact:
        return "Untitled capture"
    if len(compact) <= CAPTURE_TITLE_CHARS:
        return compact
    return compact[: CAPTURE_TITLE_CHARS - 1].rstrip(" ,;:-") + "…"


async def resolve_capture_links(
    client: Any,
    project: str | None,
    related_ids: list[str],
    task_ids: list[str],
    active_task: bool,
) -> list[str] | None:
    links = command_support.append_unique_ids(related_ids, task_ids)
    if not active_task or not project:
        return links or None

    try:
        response = await client.explore(
            mode="list",
            types=["task"],
            status="doing",
            project=project,
            limit=2,
        )
    except SibylClientError:
        return links or None

    tasks = response.get("entities", [])
    if len(tasks) != 1:
        return links or None

    task_id = tasks[0].get("id")
    if not task_id:
        return links or None

    return command_support.append_unique_ids(links, [str(task_id)])


async def write_memory_capture(
    client: Any,
    *,
    title: str,
    content: str,
    kind: str,
    domain: str | None,
    tags: list[str] | None,
    related_ids: list[str],
    task_ids: list[str],
    active_task: bool,
    effective_project: str | None,
    capture_mode: str,
    surface: str,
    wait_searchable: bool,
    memory_scope: str = "private",
    scope_key: str | None = None,
    source_id: str | None = None,
    skip_conflicts: bool = False,
    languages: list[str] | None = None,
    retrieval_keys: list[str] | None = None,
    capture_metadata: Mapping[str, Any] | None = None,
    spans: list[dict[str, Any]] | None = None,
    atomic: bool = False,
    probes: list[str] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "capture_mode": capture_mode,
        "capture_surface": surface,
        "remember_kind": kind,
    }
    if domain:
        metadata["domain"] = domain
    if languages:
        metadata["languages"] = languages
    metadata.update(capture_metadata or {})

    resolved_project = (
        await resolve_project_reference(client, effective_project) if effective_project else None
    )
    if resolved_project:
        metadata["project_id"] = resolved_project

    resolved_links = await resolve_capture_links(
        client=client,
        project=resolved_project,
        related_ids=related_ids,
        task_ids=task_ids,
        active_task=active_task,
    )
    raw_scope_key = scope_key
    if memory_scope == "project" and raw_scope_key is None:
        raw_scope_key = resolved_project

    request = MemoryCaptureRequest(
        title=title,
        content=content,
        entity_type=kind,
        domain=domain,
        tags=tags,
        related_to=resolved_links,
        languages=languages,
        metadata=metadata,
        retrieval_keys=retrieval_keys,
        provenance={
            "remember_kind": kind,
            "related_to": resolved_links or [],
        },
        source_id=source_id,
        memory_scope=memory_scope,
        scope_key=raw_scope_key,
        capture_surface=surface,
        wait_searchable=wait_searchable,
        skip_conflicts=skip_conflicts,
        spans=spans,
        atomic=atomic,
        probes=probes,
    )

    async def remember_raw_memory(capture: MemoryCaptureRequest) -> dict[str, Any]:
        return await client.remember_raw_memory(
            title=capture.title,
            raw_content=capture.content,
            source_id=capture.source_id,
            memory_scope=capture.memory_scope,
            scope_key=capture.scope_key,
            diary=capture.diary,
            agent_id=capture.agent_id,
            project_id=capture.project_id,
            tags=list(capture.tags) if capture.tags is not None else None,
            metadata=dict(capture.metadata),
            provenance=dict(capture.provenance),
            capture_surface=capture.capture_surface,
        )

    async def create_graph_entity(
        capture: MemoryCaptureRequest,
        graph_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await client.create_entity(
            name=capture.title,
            content=capture.content,
            entity_type=capture.entity_type,
            category=capture.domain,
            languages=list(capture.languages) if capture.languages is not None else None,
            tags=list(capture.tags) if capture.tags is not None else None,
            related_to=list(capture.related_to) if capture.related_to is not None else None,
            metadata=dict(graph_metadata),
            sync=capture.wait_searchable,
            skip_conflicts=capture.skip_conflicts,
            retrieval_keys=list(capture.retrieval_keys)
            if capture.retrieval_keys is not None
            else None,
            spans=[dict(span) for span in capture.spans] if capture.spans is not None else None,
            atomic=capture.atomic,
            probes=list(capture.probes) if capture.probes is not None else None,
        )

    service = MemoryCaptureService(
        remember_raw_memory=remember_raw_memory,
        create_graph_entity=create_graph_entity,
    )
    result = await service.capture(request)
    return result.to_payload()


# Repeatable, so its annotation is a container and the option object has to live
# at module scope for the default to be a name rather than a call.
PROBE_OPTION = typer.Option(
    None,
    "--probe",
    help="Question this memory must answer; repeatable, rehearsed against live search",
)


def parse_spans_json(raw: str | None) -> list[dict[str, Any]] | None:
    """Decode a --spans-json payload, faulting on anything that is not a plan.

    Only the JSON shape is checked here. The tiling rules belong to the server,
    which is the only place that knows the exact body the offsets address.
    """
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"--spans-json is not valid JSON: {exc.msg}"
        raise ValueError(msg) from exc
    if not isinstance(decoded, list) or not decoded:
        msg = '--spans-json must be a non-empty JSON array of {"start":…,"end":…} objects'
        raise ValueError(msg)
    spans: list[dict[str, Any]] = []
    for position, entry in enumerate(decoded):
        if not isinstance(entry, dict) or "start" not in entry or "end" not in entry:
            msg = f"--spans-json[{position}] must be an object with start and end"
            raise ValueError(msg)
        spans.append(cast("dict[str, Any]", entry))
    return spans


# ============================================================================
# Global callback for context override
# ============================================================================


# Leaves whose whole subject is the local config file. None of them opens a
# connection, so dropping a broken selection cannot retarget anything. Groups
# never appear here: `sibyl context` is the network recall command and
