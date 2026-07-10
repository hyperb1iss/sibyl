"""Main CLI application - client-side commands for Sibyl.

This is the entry point for the sibyl-dev package.
All commands communicate with the REST API.

Server commands (serve, dev, db, generate, etc.) are in sibyl-server.
"""

import asyncio
import re
import sys
from collections.abc import Mapping
from importlib.metadata import version as pkg_version
from os import environ
from typing import Annotated, Any, cast
from uuid import UUID

import typer
from rich.markup import escape

from sibyl_cli import config_store
from sibyl_cli.archive import app as archive_app
from sibyl_cli.auth import app as auth_app
from sibyl_cli.auth import clear_token_cmd as logout_cmd
from sibyl_cli.auth import login_cmd
from sibyl_cli.auth import status_cmd as whoami_cmd
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    NEON_CYAN,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    print_mutation_receipt,
    resolve_content_input,
    run_async,
    success,
    warn,
)
from sibyl_cli.config_cmd import app as config_app
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.context import app as context_app
from sibyl_cli.crawl import app as crawl_app
from sibyl_cli.debug import app as debug_app
from sibyl_cli.dev import app as dev_app
from sibyl_cli.docker import app as docker_app
from sibyl_cli.doctor import doctor as doctor_cmd
from sibyl_cli.document import docs_app
from sibyl_cli.entity import app as entity_app
from sibyl_cli.entity import print_entity_details
from sibyl_cli.epic import app as epic_app
from sibyl_cli.explore import app as explore_app
from sibyl_cli.host import serve as serve_cmd
from sibyl_cli.host import service_app
from sibyl_cli.host import start as start_cmd
from sibyl_cli.host import stop as stop_cmd
from sibyl_cli.id_resolution import resolve_id_prefix, resolve_raw_memory_id_prefix
from sibyl_cli.ingest import app as ingest_app
from sibyl_cli.local import app as local_app
from sibyl_cli.local import start as up_cmd
from sibyl_cli.local import stop as down_cmd
from sibyl_cli.logs import app as logs_app
from sibyl_cli.memory_display import (
    inspect_raw_memory_source,
    is_raw_memory_reference,
    print_memory_source_blame,
    print_memory_source_inspect,
    raw_memory_lookup_value,
)
from sibyl_cli.org import app as org_app
from sibyl_cli.pending import app as pending_writes_app
from sibyl_cli.project import app as project_app
from sibyl_cli.project_refs import resolve_project_reference
from sibyl_cli.session import app as session_app
from sibyl_cli.skill import app as skill_app
from sibyl_cli.state import set_context_override
from sibyl_cli.task import app as task_app
from sibyl_cli.task import list_tasks
from sibyl_cli.update import app as update_app
from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
from sibyl_core.models.context import ContextIntent
from sibyl_core.models.entities import EntityType


def get_version() -> str:
    """Get the installed package version."""
    try:
        return pkg_version("sibyl-dev")
    except Exception:
        return "unknown"


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        print(f"sibyl {get_version()}")
        raise typer.Exit()


# Main app
app = typer.Typer(
    name="sibyl",
    help="Sibyl - Oracle of Development Wisdom (CLI Client)",
    add_completion=False,
    no_args_is_help=False,
)
memory_space_app = typer.Typer(help="Memory-space inspection and preview commands")
memory_review_app = typer.Typer(help="Memory review queue automation commands")
synthesis_app = typer.Typer(help="Source-grounded synthesis commands")
team_app = typer.Typer(help="Team memory management commands")


# Register subcommand groups
app.add_typer(task_app, name="task")
app.add_typer(epic_app, name="epic")
app.add_typer(project_app, name="project")
app.add_typer(archive_app, name="archive")
app.add_typer(session_app, name="session")
app.add_typer(entity_app, name="entity")
app.add_typer(explore_app, name="explore")
app.add_typer(crawl_app, name="crawl")
app.add_typer(docs_app, name="docs")
app.add_typer(debug_app, name="debug")
app.add_typer(dev_app, name="dev")
app.add_typer(auth_app, name="auth")
app.add_typer(org_app, name="org")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")
app.add_typer(docker_app, name="docker")
app.add_typer(service_app, name="service")
app.add_typer(local_app, name="local")
app.add_typer(logs_app, name="logs")
app.add_typer(update_app, name="update")
app.add_typer(skill_app, name="skill")
app.add_typer(ingest_app, name="ingest")
app.add_typer(pending_writes_app, name="pending-writes")
app.add_typer(memory_space_app, name="memory-space")
app.add_typer(memory_review_app, name="memory-review")
app.add_typer(synthesis_app, name="synthesis")
app.add_typer(team_app, name="team")
app.command("tasks", hidden=True)(list_tasks)
app.command("doctor")(doctor_cmd)
app.command("login")(login_cmd)
app.command("logout")(logout_cmd)
app.command("serve")(serve_cmd)
app.command("start")(start_cmd)
app.command("stop")(stop_cmd)
app.command("up")(up_cmd)
app.command("down")(down_cmd)
app.command("whoami")(whoami_cmd)


SEARCH_PREVIEW_CHARS = 360
CAPTURE_TITLE_CHARS = 72
QUIET_ENV_VALUES = {"1", "true", "yes", "on"}
ENTITY_TYPE_ALIASES = {
    "gotcha": EntityType.ERROR_PATTERN.value,
    "learning": EntityType.NOTE.value,
}
ENTITY_TYPE_VALUES = [entity_type.value for entity_type in EntityType]
CONTEXT_INTENT_VALUES = [intent.value for intent in ContextIntent]
MEMORY_BASIS_VALUES = ("observed", "inferred", "told", "assumed")
MEMORY_PROPOSAL_SCOPE_VALUES = ("team",)
ENTITY_TYPE_HELP = f"Entity type: {', '.join(ENTITY_TYPE_VALUES)}"
CONTEXT_INTENT_HELP = f"Agent intent: {', '.join(CONTEXT_INTENT_VALUES)}"


def _normalize_entity_type(value: str, *, option_name: str) -> str:
    normalized = value.strip().lower()
    if alias := ENTITY_TYPE_ALIASES.get(normalized):
        warn(f"{option_name}={normalized} is deprecated; using {alias}.")
        return alias
    if normalized in ENTITY_TYPE_VALUES:
        return normalized
    choices = ", ".join(ENTITY_TYPE_VALUES)
    raise typer.BadParameter(f"{value!r} is not one of: {choices}")


def _normalize_add_type(value: str) -> str:
    return _normalize_entity_type(value, option_name="--type")


def _normalize_memory_kind(value: str) -> str:
    return _normalize_entity_type(value, option_name="--kind")


def _normalize_memory_basis(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in MEMORY_BASIS_VALUES:
        return normalized
    choices = ", ".join(MEMORY_BASIS_VALUES)
    raise typer.BadParameter(f"{value!r} is not one of: {choices}")


def _normalize_memory_proposal_scope(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in MEMORY_PROPOSAL_SCOPE_VALUES:
        return normalized
    choices = ", ".join(MEMORY_PROPOSAL_SCOPE_VALUES)
    raise typer.BadParameter(f"{value!r} is not one of: {choices}")


def _normalize_context_intent(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in CONTEXT_INTENT_VALUES:
        return normalized
    choices = ", ".join(CONTEXT_INTENT_VALUES)
    raise typer.BadParameter(f"{value!r} is not one of: {choices}")


def _looks_like_task_id(value: str) -> bool:
    candidate = value.strip()
    if candidate.startswith("task_"):
        return True
    try:
        UUID(candidate)
    except ValueError:
        return False
    return True


def _format_search_preview(content: str, max_chars: int = SEARCH_PREVIEW_CHARS) -> str:
    """Format search result previews for terminal display."""
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


def _format_highlight_preview(
    snippet: str | None,
    fallback: str,
    max_chars: int = SEARCH_PREVIEW_CHARS,
) -> str:
    raw = snippet or fallback
    preview = _format_search_preview(raw, max_chars=max_chars)
    if not snippet or ("<mark>" not in preview and "</mark>" not in preview):
        return escape(preview)

    parts = re.split(r"(<mark>|</mark>)", preview)
    active = False
    rendered: list[str] = []
    for part in parts:
        if part == "<mark>":
            active = True
            continue
        if part == "</mark>":
            active = False
            continue
        if not part:
            continue
        escaped = escape(part)
        if active:
            rendered.append(f"[bold {NEON_CYAN}]{escaped}[/]")
        else:
            rendered.append(escaped)
    return "".join(rendered)


def _derive_capture_title(content: str) -> str:
    """Create a compact default title for quick captures."""
    compact = re.sub(r"\s+", " ", content).strip()
    if not compact:
        return "Untitled capture"
    if len(compact) <= CAPTURE_TITLE_CHARS:
        return compact
    return compact[: CAPTURE_TITLE_CHARS - 1].rstrip(" ,;:-") + "…"


def _should_emit_command_marker(ctx: typer.Context) -> bool:
    if environ.get("SIBYL_QUIET", "").lower() in QUIET_ENV_VALUES:
        return False
    if ctx.invoked_subcommand in {None, "health", "brief"}:
        return False
    return not any(arg in {"--json", "-j", "--help"} for arg in sys.argv[1:])


def _emit_command_marker(ctx: typer.Context) -> None:
    if not _should_emit_command_marker(ctx):
        return
    sys.stderr.write(f"→ sibyl {ctx.invoked_subcommand}...\n")
    sys.stderr.flush()


def _parse_csv_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_id_args(values: list[str]) -> list[str]:
    ids: list[str] = []
    for value in values:
        ids = _append_unique_ids(ids, _parse_csv_ids(value))
    return ids


def _parse_section_specs(value: str | None) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    for spec in (value or "").split("|"):
        title, _, rest = spec.strip().partition("::")
        if not title:
            continue
        prompt, _, required_source_ids = rest.partition("::")
        section: dict[str, object] = {"title": title.strip()}
        if prompt.strip():
            section["prompt"] = prompt.strip()
        if required_source_ids.strip():
            section["required_source_ids"] = _parse_csv_ids(required_source_ids)
        sections.append(section)
    return sections


def _synthesis_options(
    *,
    goal: str,
    output_type: str,
    audience: str | None,
    depth: str,
    seed_query: str | None,
    project: str | None,
    all_projects: bool,
    domain: str | None,
    entity_ids: str | None,
    decision_ids: str | None,
    task_ids: str | None,
    artifact_ids: str | None,
    sections: str | None,
    constraints: str | None,
    max_sections: int,
    include_neighborhoods: bool,
) -> dict[str, Any]:
    return {
        "goal": goal,
        "output_type": output_type,
        "audience": audience,
        "depth": depth,
        "seed_query": seed_query,
        "project": project or (None if all_projects else resolve_project_from_cwd()),
        "domain": domain,
        "entity_ids": _parse_csv_ids(entity_ids),
        "decision_ids": _parse_csv_ids(decision_ids),
        "task_ids": _parse_csv_ids(task_ids),
        "artifact_ids": _parse_csv_ids(artifact_ids),
        "required_sections": _parse_section_specs(sections),
        "constraints": _parse_csv_ids(constraints),
        "max_sections": max_sections,
        "include_neighborhoods": include_neighborhoods,
    }


def _append_unique_ids(existing: list[str], additions: list[str]) -> list[str]:
    seen = set(existing)
    combined = list(existing)
    for item in additions:
        if item not in seen:
            combined.append(item)
            seen.add(item)
    return combined


async def _resolve_capture_links(
    client: Any,
    project: str | None,
    related_ids: list[str],
    task_ids: list[str],
    active_task: bool,
) -> list[str] | None:
    links = _append_unique_ids(related_ids, task_ids)
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

    return _append_unique_ids(links, [str(task_id)])


async def _write_memory_capture(
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
    capture_metadata: Mapping[str, Any] | None = None,
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

    resolved_links = await _resolve_capture_links(
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
        )

    service = MemoryCaptureService(
        remember_raw_memory=remember_raw_memory,
        create_graph_entity=create_graph_entity,
    )
    result = await service.capture(request)
    return result.to_payload()


def _print_memory_capture_result(
    *,
    title: str,
    kind: str,
    data: dict[str, Any],
    wait_searchable: bool,
) -> None:
    entity_id = data.get("id", "unknown")
    if wait_searchable:
        success(f"Remembered {kind}: {title}")
    else:
        info(f"Queued {kind}: {title}")
    print_mutation_receipt(data)
    console.print(f"  [dim]ID: {entity_id}[/dim]")
    if raw_memory_id := data.get("raw_memory_id"):
        console.print(f"  [dim]Raw: {raw_memory_id}[/dim]")
    if raw_policy_reason := data.get("raw_policy_reason"):
        console.print(f"  [dim]Policy: {raw_policy_reason}[/dim]")


def _print_reflection_persistence_summary(
    data: dict[str, object], *, persist: bool, persist_source: bool
) -> None:
    if not persist:
        return

    source_id = data.get("source_id")
    candidates = data.get("candidates")
    candidate_items = candidates if isinstance(candidates, list) else []
    persisted_ids: list[object] = []
    for item in candidate_items:
        if not isinstance(item, dict):
            continue
        candidate = cast("dict[str, object]", item)
        if persisted_id := candidate.get("persisted_id"):
            persisted_ids.append(persisted_id)
    persisted_count = data.get("persisted_count", len(persisted_ids))
    total_candidates = data.get("total_candidates", len(candidate_items))

    console.print()
    if persist_source:
        if source_id:
            success(f"Persisted source: {source_id}")
        else:
            info("Persisted source: unavailable")
    else:
        info("Source persistence skipped (--no-source)")

    success(f"Persisted candidates: {persisted_count}/{total_candidates}")
    for persisted_id in persisted_ids:
        console.print(f"  [dim]ID: {persisted_id}[/dim]")


def _print_raw_memory_results(memories: list[object]) -> None:
    if not memories:
        info("No raw memories found")
        return

    console.print(f"\n[bold]Found {len(memories)} raw memories:[/bold]\n")
    for item in memories:
        if not isinstance(item, dict):
            continue
        memory = cast("dict[str, object]", item)
        title = str(memory.get("title") or "Untitled raw memory")
        source_id = str(memory.get("source_id") or "")
        memory_id = str(memory.get("id") or "")
        content = str(memory.get("raw_content") or "")
        snippet = str(memory.get("snippet") or "")
        score = memory.get("score")
        scope = str(memory.get("memory_scope") or "private")
        policy_reason = str(memory.get("policy_reason") or "")

        source_label = f" [dim]({source_id})[/dim]" if source_id else ""
        console.print(f"  [{NEON_CYAN}]{title}[/{NEON_CYAN}]{source_label}")
        if content or snippet:
            console.print(
                f"    {_format_highlight_preview(snippet or None, content)}",
                soft_wrap=True,
            )
        score_label = f" score={score}" if score else ""
        policy_label = f" policy={policy_reason}" if policy_reason else ""
        console.print(f"    [dim]scope={scope}{score_label}{policy_label}[/dim]")
        console.print(f"    [{CORAL}]{memory_id}[/{CORAL}]")
        console.print()


def _int_value(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast("dict[str, object]", item) for item in value if isinstance(item, dict)]


def _source_pack_receipt_counts(data: dict[str, object]) -> tuple[int, int, int, int]:
    hidden = 0
    redacted = 0
    corrected = 0
    freshness = 0
    for pack in _dict_list(data.get("source_packs")):
        hidden += _int_value(pack.get("hidden_count"))
        redacted += _int_value(pack.get("redaction_count"))
        corrected += _int_value(pack.get("correction_count"))
        freshness_payload = pack.get("freshness")
        if isinstance(freshness_payload, dict):
            freshness += len(freshness_payload)
    return hidden, redacted, corrected, freshness


def _source_pack_correction_reasons(data: dict[str, object]) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()
    for pack in _dict_list(data.get("source_packs")):
        for reason in _correction_reason_names(pack.get("correction_reasons")):
            if reason not in seen:
                reasons.append(reason)
                seen.add(reason)
    return reasons


def _correction_reason_names(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(reason) for reason in value]
    if isinstance(value, list):
        return [str(reason) for reason in value]
    return []


def _find_source_pack(data: dict[str, object], section_id: object) -> dict[str, object] | None:
    for pack in _dict_list(data.get("source_packs")):
        if pack.get("section_id") == section_id:
            return pack
    return None


def _print_source_pack_receipt(pack: dict[str, object]) -> None:
    source_ids = pack.get("source_ids")
    source_count = len(source_ids) if isinstance(source_ids, list) else 0
    hidden = _int_value(pack.get("hidden_count"))
    redacted = _int_value(pack.get("redaction_count"))
    corrected = _int_value(pack.get("correction_count"))
    if source_count:
        console.print(f"    [dim]sources: {source_count} receipt(s)[/dim]")
    if hidden or redacted or corrected:
        console.print(
            f"    [dim]impact: {hidden} hidden · {redacted} redacted · {corrected} corrected[/dim]"
        )
    reasons = _correction_reason_names(pack.get("correction_reasons"))
    if reasons:
        console.print(f"    [dim]corrections: {', '.join(reasons[:4])}[/dim]")


def _print_synthesis_plan(data: dict[str, object]) -> None:
    outline = cast("dict[str, object]", data.get("outline") or {})
    title = str(outline.get("title") or "Synthesis Plan")
    sections = outline.get("sections")
    section_items = sections if isinstance(sections, list) else []
    verification = cast("dict[str, object]", data.get("verification") or {})
    console.print(f"\n[bold]{title}[/bold]")
    console.print(
        f"[dim]Run: {data.get('run_id')} · "
        f"verification={verification.get('status')} · "
        f"sources={verification.get('source_count', 0)}[/dim]\n"
    )
    for item in section_items:
        if not isinstance(item, dict):
            continue
        section = cast("dict[str, object]", item)
        source_ids = section.get("source_ids")
        source_count = len(source_ids) if isinstance(source_ids, list) else 0
        console.print(f"  [{NEON_CYAN}]{section.get('title')}[/{NEON_CYAN}]")
        console.print(f"    [dim]{source_count} source(s)[/dim]")
        gaps = section.get("gaps")
        for gap in gaps if isinstance(gaps, list) else []:
            if isinstance(gap, dict):
                gap_data = cast("dict[str, object]", gap)
                console.print(f"    [dim]gap: {gap_data.get('reason')}[/dim]")
        if pack := _find_source_pack(data, section.get("section_id")):
            _print_source_pack_receipt(pack)


def _print_synthesis_verification(data: dict[str, object]) -> None:
    verification = cast("dict[str, object]", data.get("verification") or {})
    status = str(verification.get("status") or "unknown")
    source_count = verification.get("source_count", 0)
    gap_count = verification.get("gap_count", 0)
    if status == "pass":
        success(f"Synthesis verification passed ({source_count} sources)")
    else:
        error(f"Synthesis verification has gaps ({gap_count})")
    gaps = verification.get("gaps")
    for gap in gaps if isinstance(gaps, list) else []:
        if isinstance(gap, dict):
            gap_data = cast("dict[str, object]", gap)
            console.print(f"  [dim]{gap_data.get('title')}: {gap_data.get('reason')}[/dim]")
    hidden, redacted, corrected, freshness = _source_pack_receipt_counts(data)
    if hidden or redacted or corrected or freshness:
        console.print(
            f"  [dim]Correction impact: {hidden} hidden · {redacted} redacted · "
            f"{corrected} corrected · {freshness} freshness[/dim]"
        )
    reasons = _source_pack_correction_reasons(data)
    if reasons:
        console.print(f"  [dim]Correction reasons: {', '.join(reasons[:5])}[/dim]")


def _print_synthesis_artifact(data: dict[str, object], *, output_format: str) -> None:
    artifact = cast("dict[str, object]", data.get("artifact") or {})
    if output_format == "json":
        print_json(cast("dict[str, object]", artifact.get("json_payload") or {}))
        return
    console.print(str(artifact.get("markdown") or ""))


def _print_synthesis_remember(data: dict[str, object]) -> None:
    artifact = cast("dict[str, object]", data.get("artifact") or {})
    remembered_memory_id = artifact.get("remembered_memory_id")
    remembered_source_id = artifact.get("remembered_source_id")
    if remembered_memory_id:
        success(f"Remembered synthesis artifact: {artifact.get('title')}")
        console.print(f"  [dim]Artifact: {artifact.get('artifact_id', '')}[/dim]")
        console.print(f"  [dim]Memory: {remembered_memory_id}[/dim]")
        console.print(f"  [dim]Source: {remembered_source_id}[/dim]")
        source_ids = artifact.get("source_ids")
        if isinstance(source_ids, list) and source_ids:
            console.print(
                f"  [dim]Source receipts: {', '.join(str(item) for item in source_ids)}[/dim]"
            )
        return
    error("Synthesis artifact was drafted but not remembered.")


def _source_import_scope(data: dict[str, object]) -> str:
    scope = str(data.get("target_memory_scope") or "private")
    if scope_key := data.get("target_scope_key"):
        return f"{scope}:{scope_key}"
    return scope


def _source_import_progress(data: dict[str, object]) -> dict[str, object]:
    progress = data.get("progress")
    return cast("dict[str, object]", progress) if isinstance(progress, dict) else {}


def _source_import_safe_record_summary(record: dict[str, object]) -> str:
    for key in ("adapter_record_id", "source_uri", "code", "type"):
        if value := record.get(key):
            return str(value)
    return "record"


def _print_source_import_status(data: dict[str, object]) -> None:
    progress = _source_import_progress(data)
    console.print("\n[bold]Source import receipt[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Import Id", str(data.get("import_id") or ""))
    table.add_row("Status", str(data.get("status") or ""))
    table.add_row("Adapter", str(data.get("adapter_name") or ""))
    table.add_row("Source", str(data.get("source_identity") or ""))
    table.add_row("Target scope", _source_import_scope(data))
    table.add_row("Privacy", str(data.get("privacy_class") or "default"))
    table.add_row("Imported", str(progress.get("imported_count") or 0))
    table.add_row("Skipped", str(progress.get("skipped_count") or 0))
    table.add_row("Deduped", str(progress.get("dedupe_count") or 0))
    table.add_row("Errors", str(progress.get("error_count") or 0))
    table.add_row("Attachments", str(progress.get("attachment_count") or 0))
    table.add_row("Pending extraction", str(progress.get("extraction_pending_count") or 0))
    console.print(table)

    raw_memory_ids = data.get("raw_memory_ids")
    if isinstance(raw_memory_ids, list) and raw_memory_ids:
        console.print("\n[bold]Raw memory receipts[/bold]")
        for source_id in raw_memory_ids[:18]:
            console.print(f"  [{CORAL}]{source_id}[/{CORAL}]")

    skipped_records = _dict_list(data.get("skipped_records"))
    if skipped_records:
        console.print("\n[bold]Skipped records[/bold]")
        for record in skipped_records[:6]:
            reason = record.get("reason") or record.get("message") or "skipped"
            console.print(f"  [dim]{_source_import_safe_record_summary(record)}: {reason}[/dim]")

    errors = _dict_list(data.get("errors"))
    if errors:
        console.print("\n[bold]Errors[/bold]")
        for record in errors[:6]:
            message = record.get("message") or record.get("error") or "error"
            console.print(f"  [dim]{_source_import_safe_record_summary(record)}: {message}[/dim]")


def _parse_policy_filter(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"allow", "allowed", "true", "1", "yes"}:
        return True
    if normalized in {"deny", "denied", "false", "0", "no"}:
        return False
    error("Policy filter must be allowed or denied.")
    raise typer.Exit(code=1)


def _format_policy_state(value: object) -> str:
    if value is True:
        return "allowed"
    if value is False:
        return "denied"
    return "n/a"


def _audit_id_summary(value: object, truncated: object = None) -> str:
    if not isinstance(value, list) or not value:
        return ""
    ids = [str(item) for item in value[:2]]
    stored_remainder = max(len(value) - 2, 0)
    hidden_count = (
        truncated if isinstance(truncated, int) and not isinstance(truncated, bool) else 0
    )
    remaining = stored_remainder + hidden_count
    if remaining:
        ids.append(f"+{remaining}")
    return ", ".join(ids)


def _print_memory_audit_events(events: list[object]) -> None:
    if not events:
        info("No memory audit events found")
        return

    table = create_table(
        "Memory Audit",
        "Time",
        "Action",
        "Policy",
        "Scope",
        "Source",
        "Derived",
        expand=False,
    )
    table.columns[0].no_wrap = True
    table.columns[1].no_wrap = True
    for item in events:
        if not isinstance(item, dict):
            continue
        event = cast("dict[str, object]", item)
        created_at = str(event.get("created_at") or "")
        timestamp = created_at.replace("T", " ")[:19]
        scope = str(event.get("memory_scope") or "")
        scope_key = str(event.get("scope_key") or "")
        if scope_key:
            scope = f"{scope}:{scope_key}" if scope else scope_key
        table.add_row(
            timestamp,
            str(event.get("action") or ""),
            _format_policy_state(event.get("policy_allowed")),
            scope,
            _audit_id_summary(event.get("source_ids"), event.get("source_ids_truncated")),
            _audit_id_summary(event.get("derived_ids"), event.get("derived_ids_truncated")),
        )
    console.print(table)


def _preview_state(value: object) -> str:
    return "allowed" if value is True else "denied"


def _access_preview_state(data: dict[str, object]) -> str:
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        state = cast("dict[str, object]", metadata).get("access_state")
        if state in {"allowed", "partial", "denied"}:
            return str(state)
    return _preview_state(data.get("allowed"))


def _preview_target(scope: object, scope_key: object) -> str:
    target = str(scope or "default")
    if scope_key:
        target = f"{target}:{scope_key}"
    return target


def _preview_id_summary(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "-"
    return ", ".join(str(item) for item in value)


def _preview_count(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "0"


def _preview_audit_id(data: dict[str, object]) -> str:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    payload = cast("dict[str, object]", metadata)
    for key in ("audit_id", "audit_event_id", "receipt_id"):
        if audit_id := payload.get(key):
            return str(audit_id)
    return ""


def _print_promotion_preview(data: dict[str, object]) -> None:
    console.print("\n[bold]Promotion preview[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", _preview_state(data.get("allowed")))
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Candidate", str(data.get("candidate_id") or ""))
    table.add_row("Review", str(data.get("review_state") or ""))
    table.add_row(
        "Target",
        _preview_target(data.get("promote_to_scope"), data.get("promote_to_scope_key")),
    )
    table.add_row("Sources", _preview_id_summary(data.get("raw_source_ids")))
    table.add_row("Reasons", _preview_id_summary(data.get("policy_reasons")))
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def _print_promotion_result(data: dict[str, object]) -> None:
    console.print("\n[bold]Promotion result[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", "promoted" if data.get("success") is True else "blocked")
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Candidate", str(data.get("candidate_id") or ""))
    table.add_row("Review", str(data.get("review_state") or ""))
    table.add_row(
        "Target",
        _preview_target(data.get("memory_scope"), data.get("scope_key")),
    )
    table.add_row("Sources", _preview_id_summary(data.get("raw_source_ids")))
    table.add_row("Policy", _preview_id_summary(data.get("policy_reasons")))
    if promoted_id := data.get("promoted_id"):
        table.add_row("Promoted", str(promoted_id))
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def _print_promotion_autonomy(data: dict[str, object]) -> None:
    console.print("\n[bold]Automatic memory review[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Outcome", str(data.get("outcome") or ""))
    table.add_row("Action", str(data.get("recommended_action") or ""))
    table.add_row("Applied", "yes" if data.get("applied") is True else "no")
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Candidate", str(data.get("candidate_id") or ""))
    table.add_row("Review", str(data.get("review_state") or ""))
    table.add_row(
        "Target",
        _preview_target(data.get("promote_to_scope"), data.get("promote_to_scope_key")),
    )
    table.add_row("Sources", _preview_id_summary(data.get("raw_source_ids")))
    table.add_row("Exceptions", _preview_id_summary(data.get("exception_reasons")))
    table.add_row("Policy", _preview_id_summary(data.get("policy_reasons")))
    if promoted_id := data.get("promoted_id"):
        table.add_row("Promoted", str(promoted_id))
    if data.get("dry_run") is True:
        table.add_row("Dry run", "yes")
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def _print_memory_review_drain(data: dict[str, object]) -> None:
    console.print("\n[bold]Memory review drain[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Mode", "dry-run" if data.get("dry_run") is True else "apply")
    table.add_row("Scanned", _preview_count(data.get("scanned_count")))
    table.add_row("Auto-promote", _preview_count(data.get("auto_promote_count")))
    table.add_row("Applied", _preview_count(data.get("applied_count")))
    table.add_row("Exceptions", _preview_count(data.get("exception_count")))
    table.add_row("Archived", _preview_count(data.get("archived_count")))
    table.add_row("Skipped", _preview_count(data.get("skip_count")))
    table.add_row("Failed", _preview_count(data.get("failed_count")))
    console.print(table)

    results = data.get("results")
    if not isinstance(results, list) or not results:
        return

    result_table = create_table(
        "Drain Results",
        "Candidate",
        "Outcome",
        "Action",
        "State",
        "Reason",
        "Promoted",
        "Archived",
        expand=False,
    )
    for item in results:
        if not isinstance(item, dict):
            continue
        row = cast("dict[str, object]", item)
        result_table.add_row(
            str(row.get("candidate_id") or ""),
            str(row.get("outcome") or ""),
            str(row.get("recommended_action") or ""),
            str(row.get("review_state") or ""),
            str(row.get("reason") or row.get("error") or ""),
            str(row.get("promoted_id") or "-"),
            "yes" if row.get("archived") is True else "no",
        )
    console.print(result_table)


def _print_reflection_dream_enqueue(data: dict[str, object], *, dry_run: bool) -> None:
    console.print("\n[bold]Reflection dream cycle[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Mode", "dry-run" if dry_run else "apply")
    table.add_row("Job", str(data.get("job_id") or ""))
    table.add_row("Function", str(data.get("function") or ""))
    table.add_row("Status", str(data.get("status") or ""))
    table.add_row("Message", str(data.get("message") or ""))
    console.print(table)


def _job_time(job: dict[str, object]) -> str:
    for key in ("finish_time", "start_time", "enqueue_time"):
        if value := job.get(key):
            return str(value).replace("T", " ")[:19]
    return ""


def _event_time(event: dict[str, object]) -> str:
    return str(event.get("created_at") or "").replace("T", " ")[:19]


def _dream_action_label(value: object) -> str:
    action = str(value or "").removeprefix("memory.reflect.")
    if action == "dream_promote":
        return "promote"
    if action == "dream_review":
        return "review"
    return action


def _print_reflection_dream_status(data: dict[str, object]) -> None:
    jobs = data.get("jobs")
    events = data.get("events")
    job_items = jobs if isinstance(jobs, list) else []
    event_items = events if isinstance(events, list) else []

    if not job_items and not event_items:
        info("No reflection dream-cycle receipts found")
        return

    if job_items:
        table = create_table(
            "Reflection Dream Runs",
            "Time",
            "Status",
            "Job",
            expand=False,
        )
        for item in job_items:
            if not isinstance(item, dict):
                continue
            job = cast("dict[str, object]", item)
            table.add_row(
                _job_time(job),
                str(job.get("status") or ""),
                str(job.get("job_id") or ""),
            )
        console.print(table)

    if event_items:
        table = create_table(
            "Reflection Dream Receipts",
            "Time",
            "Action",
            "Policy",
            "Scope",
            "Source",
            "Derived",
            expand=False,
        )
        for item in event_items:
            if not isinstance(item, dict):
                continue
            event = cast("dict[str, object]", item)
            scope = str(event.get("memory_scope") or "")
            scope_key = str(event.get("scope_key") or "")
            if scope_key:
                scope = f"{scope}:{scope_key}" if scope else scope_key
            table.add_row(
                _event_time(event),
                _dream_action_label(event.get("action")),
                _format_policy_state(event.get("policy_allowed")),
                scope,
                _audit_id_summary(event.get("source_ids"), event.get("source_ids_truncated")),
                _audit_id_summary(event.get("derived_ids"), event.get("derived_ids_truncated")),
            )
        console.print(table)


def _print_share_preview(data: dict[str, object]) -> None:
    console.print("\n[bold]Share preview[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", _preview_state(data.get("allowed")))
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Target", _preview_target(data.get("target_scope"), data.get("target_scope_key")))
    table.add_row("Sources", _preview_id_summary(data.get("source_ids")))
    table.add_row("Visible", _preview_id_summary(data.get("visible_source_ids")))
    table.add_row("Denied", _preview_id_summary(data.get("denied_source_ids")))
    table.add_row("Missing", _preview_id_summary(data.get("missing_source_ids")))
    table.add_row("Redacted", _preview_count(data.get("redacted_count")))
    table.add_row("Hidden relevant", _preview_count(data.get("hidden_but_relevant_count")))
    table.add_row("Reasons", _preview_id_summary(data.get("policy_reasons")))
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def _print_share_result(data: dict[str, object]) -> None:
    console.print("\n[bold]Share result[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", "applied" if data.get("applied") is True else "blocked")
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row("Target", _preview_target(data.get("target_scope"), data.get("target_scope_key")))
    table.add_row("Sources", _preview_id_summary(data.get("source_ids")))
    table.add_row("Visible", _preview_id_summary(data.get("visible_source_ids")))
    table.add_row("Denied", _preview_id_summary(data.get("denied_source_ids")))
    table.add_row("Promoted", _preview_id_summary(data.get("promoted_ids")))
    table.add_row("Audit", _preview_id_summary(data.get("audit_event_ids")))
    console.print(table)


def _print_access_preview(data: dict[str, object]) -> None:
    console.print("\n[bold]Access preview[/bold]\n")
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("State", _access_preview_state(data))
    table.add_row("Reason", str(data.get("reason") or ""))
    table.add_row(
        "Target",
        _preview_target(data.get("target_principal_type"), data.get("target_principal_id")),
    )
    table.add_row("Spaces", _preview_id_summary(data.get("memory_space_ids")))
    table.add_row("Visible", _preview_id_summary(data.get("visible_source_ids")))
    table.add_row("Denied", _preview_id_summary(data.get("denied_source_ids")))
    table.add_row("Redacted", _preview_count(data.get("redacted_count")))
    table.add_row("Hidden relevant", _preview_count(data.get("hidden_but_relevant_count")))
    table.add_row("Reasons", _preview_id_summary(data.get("policy_reasons")))
    if audit_id := _preview_audit_id(data):
        table.add_row("Audit", audit_id)
    console.print(table)


def _team_count(value: object) -> str:
    return str(len(value)) if isinstance(value, list) else "0"


def _print_team_list(data: dict[str, object]) -> None:
    teams = data.get("teams")
    rows = teams if isinstance(teams, list) else []
    table = create_table("Team", "Slug", "Scope", "Memory space")
    for item in rows:
        if not isinstance(item, dict):
            continue
        team = cast("dict[str, object]", item)
        table.add_row(
            str(team.get("name") or ""),
            str(team.get("slug") or ""),
            str(team.get("memory_scope_key") or team.get("id") or ""),
            str(team.get("memory_space_id") or ""),
        )
    console.print(table)


def _print_team(data: dict[str, object]) -> None:
    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("Team", str(data.get("name") or ""))
    table.add_row("Slug", str(data.get("slug") or ""))
    table.add_row("ID", str(data.get("id") or ""))
    table.add_row("Memory space", str(data.get("memory_space_id") or ""))
    table.add_row("Scope key", str(data.get("memory_scope_key") or ""))
    table.add_row("Members", _team_count(data.get("members")))
    table.add_row("Projects", _team_count(data.get("projects")))
    console.print(table)


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if e.error_code or e.request_id or e.remediation:
        handle_client_error(e)
    elif "Cannot connect" in str(e):
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Cannot connect to Sibyl server[/bold]")
        console.print()
        console.print(f"    [{NEON_CYAN}]›[/{NEON_CYAN}] Check that the Sibyl server is running")
        console.print()
    elif e.status_code == 401:
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Authentication required[/bold]")
        console.print()
        console.print(
            f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl auth login[/bold {NEON_CYAN}]   [dim]Log in[/dim]"
        )
        console.print(
            f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl auth signup[/bold {NEON_CYAN}]  [dim]Create account[/dim]"
        )
        console.print()
    elif e.status_code == 403:
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Access denied[/bold]")
        if e.detail:
            console.print()
            console.print(f"    [{NEON_CYAN}]›[/{NEON_CYAN}] {e.detail}")
        console.print()
    else:
        handle_client_error(e)
    raise typer.Exit(1)


# ============================================================================
# Global callback for context override
# ============================================================================


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            "-C",
            help="Use this named context (server/org bundle) for this command",
            envvar="SIBYL_CONTEXT",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Sibyl CLI - interact with your knowledge graph."""
    if context:
        set_context_override(context)

    _emit_command_marker(ctx)

    # Show help if no command
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


# ============================================================================
# Root-level commands
# ============================================================================


async def _load_show_reference(client: Any, reference: str) -> tuple[str, dict[str, object]]:
    if is_raw_memory_reference(reference):
        return "raw_memory", await inspect_raw_memory_source(client, reference)

    entity_error: SibylClientError | None = None
    try:
        resolved_id = await resolve_id_prefix(client, reference)
        entity = await client.get_entity(resolved_id)
        return "entity", cast("dict[str, object]", entity)
    except SibylClientError as e:
        if e.status_code != 404:
            raise
        entity_error = e

    try:
        return "raw_memory", await inspect_raw_memory_source(client, reference)
    except SibylClientError as e:
        if e.status_code == 404:
            detail = f"No entity or raw memory matches: {reference}"
            raise SibylClientError(detail, status_code=404, detail=detail) from entity_error
        raise


@app.command("show")
def show_reference(
    reference: Annotated[str, typer.Argument(help="Entity or raw memory ID")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show an entity or raw memory by ID."""

    @run_async
    async def _show() -> None:
        try:
            async with get_client() as client:
                kind, data = await _load_show_reference(client, reference)

            if json_out:
                print_json(data)
                return

            if kind == "raw_memory":
                print_memory_source_inspect(data, full_content=True)
            else:
                print_entity_details(data)
        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command()
def health(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Check Sibyl server health."""

    @run_async
    async def check_health() -> None:
        try:
            async with get_client() as client:
                data = await client.get("/health")

                if json_output:
                    print_json(data)
                    return
                status = data.get("status", "unknown")
                server = data.get("server_name", "sibyl")

                if status == "healthy":
                    success(f"{server} is healthy")
                    if counts := data.get("counts"):
                        console.print(f"  [dim]Entities: {counts.get('entities', 0)}[/dim]")
                        console.print(
                            f"  [dim]Relationships: {counts.get('relationships', 0)}[/dim]"
                        )
                else:
                    error(f"{server} is unhealthy: {status}")
                    raise typer.Exit(1)
        except SibylClientError as e:
            _handle_client_error(e)

    check_health()


@app.command("init")
def init_cmd(
    remote: Annotated[
        str | None,
        typer.Option("--remote", help="Remote Sibyl server URL for CLI-only mode"),
    ] = None,
    local: Annotated[
        bool,
        typer.Option("--local", help="Create a localhost context"),
    ] = False,
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Context name"),
    ] = None,
    org: Annotated[str, typer.Option("--org", "-o", help="Organization slug")] = "",
    project: Annotated[str, typer.Option("--project", "-p", help="Default project ID")] = "",
    insecure: Annotated[
        bool, typer.Option("--insecure", "-k", help="Skip SSL verification for this context")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Update an existing context")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
) -> None:
    """Create an explicit local or remote context for first-run setup."""
    if remote and local:
        error("--remote and --local cannot be combined")
        raise typer.Exit(1)

    server_url = remote or "http://localhost:3334"
    context_name = name or ("remote" if remote else "local")
    existing = config_store.get_context(context_name)

    try:
        if existing:
            if not force:
                error(f"Context '{context_name}' already exists. Use --force to update it.")
                raise typer.Exit(1)
            ctx = config_store.update_context(
                context_name,
                server_url=server_url,
                org_slug=org or None,
                default_project=project or None,
                insecure=insecure,
            )
            config_store.set_active_context(context_name)
            action = "updated"
        else:
            ctx = config_store.create_context(
                context_name,
                server_url=server_url,
                org_slug=org or None,
                default_project=project or None,
                set_active=True,
                insecure=insecure,
            )
            action = "created"
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from None

    if json_output:
        print_json(
            {
                "context": context_name,
                "server_url": ctx.server_url,
                "org_slug": ctx.org_slug,
                "default_project": ctx.default_project,
                "active": True,
                "mode": "remote" if remote else "local",
                "action": action,
            }
        )
        return

    success(f"{action.capitalize()} context '{context_name}'")
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}]  {ctx.server_url}")
    console.print(f"  [{NEON_CYAN}]Org:[/{NEON_CYAN}]     {ctx.org_slug or '[dim]auto[/dim]'}")
    console.print(
        f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}] {ctx.default_project or '[dim]none[/dim]'}"
    )
    console.print()
    if remote:
        info("Next: sibyl auth login && sibyl doctor")
    else:
        info("Next: sibyl serve, then sibyl doctor")


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    entity_type: str | None = typer.Option(None, "--type", "-t", help="Filter by entity type"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Search all projects"),
    graph_only: bool = typer.Option(False, "--graph-only", help="Search graph memory only"),
    docs_only: bool = typer.Option(False, "--docs-only", help="Search crawled docs only"),
    as_of: str | None = typer.Option(None, "--as-of", help="Filter graph memory as of a timestamp"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Search the knowledge graph."""
    if graph_only and docs_only:
        error("--graph-only and --docs-only cannot be combined")
        raise typer.Exit(1)

    normalized_type = entity_type.lower() if entity_type else None
    if graph_only and normalized_type == "document":
        error("--graph-only cannot be combined with --type document")
        raise typer.Exit(1)
    if docs_only and normalized_type and normalized_type != "document":
        error("--docs-only can only be combined with --type document")
        raise typer.Exit(1)

    # Auto-resolve project from context unless --all
    effective_project = None if all_projects else resolve_project_from_cwd()
    include_documents = not graph_only
    include_graph = not docs_only

    @run_async
    async def run_search() -> None:
        try:
            async with get_client() as client:
                types = [entity_type] if entity_type else None
                search_kwargs: dict[str, Any] = {}
                if as_of:
                    search_kwargs["as_of"] = as_of
                data = await client.search(
                    query,
                    types=types,
                    limit=limit,
                    project=effective_project,
                    include_documents=include_documents,
                    include_graph=include_graph,
                    **search_kwargs,
                )

                if json_output:
                    print_json(data)
                    return

                results = data.get("results", [])
                if not results:
                    info("No results found")
                    return

                console.print(f"\n[bold]Found {len(results)} results:[/bold]\n")
                for r in results:
                    entity_id = r.get("id", "")
                    name = r.get("name", "Unknown")
                    source = r.get("source")
                    content = r.get("content", "")
                    metadata = r.get("metadata", {})
                    heading_path = metadata.get("heading_path", [])
                    origin = str(
                        r.get("result_origin")
                        or ("document" if metadata.get("document_id") else "graph")
                    ).lower()
                    origin_label = {
                        "document": "docs",
                        "raw_memory": "memory",
                    }.get(origin, "graph")

                    # Header: Document name (source)
                    # Skip file paths - they're not useful. Show source name only.
                    display_source = source if source and not source.startswith("/") else None
                    source_info = f" ({display_source})" if display_source else ""
                    console.print(
                        f"  [dim]{origin_label}[/dim] "
                        f"[{NEON_CYAN}]{name}[/{NEON_CYAN}][dim]{source_info}[/dim]"
                    )

                    # Section path
                    if heading_path:
                        path_str = " > ".join(heading_path)
                        console.print(f"    [dim]{path_str}[/dim]")

                    # Content preview
                    if content:
                        metadata_snippet = metadata.get("snippet")
                        snippet = (
                            metadata_snippet
                            if isinstance(metadata_snippet, str)
                            else content
                            if "<mark>" in content
                            else None
                        )
                        console.print(
                            f"    {_format_highlight_preview(snippet, content)}",
                            soft_wrap=True,
                        )

                    # Show IDs for fetching
                    document_id = metadata.get("document_id")
                    if document_id:
                        # Crawled doc: show document_id for full doc retrieval
                        console.print(f"    [dim]doc:[/dim] [{CORAL}]{document_id}[/{CORAL}]")
                    else:
                        # Graph entity: show entity ID
                        console.print(f"    [{CORAL}]{entity_id}[/{CORAL}]")
                    console.print()

                # Hint for retrieval - check if any results are from crawled docs
                has_docs = any(r.get("metadata", {}).get("document_id") for r in results)
                has_entities = any(not r.get("metadata", {}).get("document_id") for r in results)

                hints = []
                if has_entities:
                    hints.append(f"[{NEON_CYAN}]sibyl show <id>[/{NEON_CYAN}]")
                if has_docs:
                    hints.append(f"[{NEON_CYAN}]sibyl crawl documents show <doc>[/{NEON_CYAN}]")

                if hints:
                    console.print(f"[dim]Full content:[/dim] {' [dim]or[/dim] '.join(hints)}")
        except SibylClientError as e:
            _handle_client_error(e)

    run_search()


@app.command("add")
def add_knowledge(
    title: str | None = typer.Argument(None, help="Title/name of the knowledge"),
    content: str | None = typer.Argument(None, help="Content/description"),
    title_option: str | None = typer.Option(None, "--title", help="Title/name of the knowledge"),
    content_option: str | None = typer.Option(None, "--content", help="Content/description"),
    content_file: str | None = typer.Option(None, "--content-file", help="Read content from file"),
    max_size: int = typer.Option(
        1_048_576,
        "--max-size",
        min=1,
        help="Maximum content file size in bytes",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Allow --content-file to read through symlinks",
    ),
    entity_type: str = typer.Option(
        "episode",
        "--type",
        "-t",
        callback=_normalize_add_type,
        help=ENTITY_TYPE_HELP,
    ),
    category: str | None = typer.Option(None, "--category", "-c", help="Category"),
    language: str | None = typer.Option(None, "--language", "-l", help="Language"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help="Comma-separated entity IDs to connect with RELATED_TO edges",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to connect with RELATED_TO edges",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="Auto-link to the single active task in the current project",
    ),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new entity is persisted and ready for direct retrieval",
    ),
    skip_conflicts: bool = typer.Option(
        False,
        "--skip-conflicts",
        "--no-conflict-check",
        help="Skip semantic duplicate/conflict detection",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add knowledge to the graph."""
    resolved_title = (title_option or title or "").strip()
    try:
        resolved_content = (
            resolve_content_input(
                content_option if content_option is not None else content,
                content_file=content_file,
                max_size=max_size,
                follow_symlinks=follow_symlinks,
            )
            or ""
        ).strip()
    except ValueError as e:
        error(str(e))
        raise typer.Exit(code=1) from e
    if not resolved_title:
        error("Provide a title as an argument or with --title.")
        raise typer.Exit(code=1)
    if not resolved_content:
        error("Provide content as an argument, via stdin, or with --content-file.")
        raise typer.Exit(code=1)
    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    related_ids = _parse_csv_ids(related_to)
    task_ids = _parse_csv_ids(task)
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_add() -> None:
        try:
            async with get_client() as client:
                data = await _write_memory_capture(
                    client,
                    title=resolved_title,
                    content=resolved_content,
                    kind=entity_type,
                    domain=category,
                    tags=parsed_tags,
                    related_ids=related_ids,
                    task_ids=task_ids,
                    active_task=active_task,
                    effective_project=effective_project,
                    capture_mode="add",
                    surface="cli",
                    wait_searchable=wait_searchable,
                    skip_conflicts=skip_conflicts,
                    languages=[language] if language else None,
                )

                if json_output:
                    print_json(data)
                    return

                _print_memory_capture_result(
                    title=resolved_title,
                    kind=entity_type,
                    data=data,
                    wait_searchable=wait_searchable,
                )
        except SibylClientError as e:
            _handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e

    run_add()


@app.command("capture")
def capture_memory(
    content: str | None = typer.Argument(
        None,
        help="What to capture. Reads stdin if omitted.",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        "-t",
        help="Optional title. Derived from content when omitted.",
    ),
    entity_type: str = typer.Option(
        "episode",
        "--type",
        callback=_normalize_add_type,
        help=ENTITY_TYPE_HELP,
    ),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help="Comma-separated entity IDs to connect with RELATED_TO edges",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to connect with RELATED_TO edges",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="Auto-link to the single active task in the current project",
    ),
    content_file: str | None = typer.Option(None, "--content-file", help="Read content from file"),
    max_size: int = typer.Option(
        1_048_576,
        "--max-size",
        min=1,
        help="Maximum content file size in bytes",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Allow --content-file to read through symlinks",
    ),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new entity is persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Capture a quick memory without separate title and content fields."""

    try:
        resolved_content = (
            resolve_content_input(
                content,
                content_file=content_file,
                max_size=max_size,
                follow_symlinks=follow_symlinks,
            )
            or ""
        ).strip()
    except ValueError as e:
        error(str(e))
        raise typer.Exit(code=1) from e
    if not resolved_content:
        error("Provide capture content as an argument, via stdin, or with --content-file.")
        raise typer.Exit(code=1)

    resolved_title = (title or "").strip() or _derive_capture_title(resolved_content)
    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    related_ids = _parse_csv_ids(related_to)
    task_ids = _parse_csv_ids(task)
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_capture() -> None:
        try:
            async with get_client() as client:
                data = await _write_memory_capture(
                    client,
                    title=resolved_title,
                    content=resolved_content,
                    kind=entity_type,
                    domain=None,
                    tags=parsed_tags,
                    related_ids=related_ids,
                    task_ids=task_ids,
                    active_task=active_task,
                    effective_project=effective_project,
                    capture_mode="quick",
                    surface="cli",
                    wait_searchable=wait_searchable,
                )

                if json_output:
                    print_json(data)
                    return

                _print_memory_capture_result(
                    title=resolved_title,
                    kind=entity_type,
                    data=data,
                    wait_searchable=wait_searchable,
                )
        except SibylClientError as e:
            _handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e

    run_capture()


@app.command("note")
def note_alias(
    subject: str = typer.Argument(..., help="Task ID for task notes, or free note content"),
    content: str | None = typer.Argument(None, help="Note body or '-' for stdin"),
    content_file: str | None = typer.Option(
        None, "--content-file", help="Read note content from file"
    ),
    max_size: int = typer.Option(
        1_048_576,
        "--max-size",
        min=1,
        help="Maximum content file size in bytes",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Allow --content-file to read through symlinks",
    ),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help="Comma-separated entity IDs to connect with RELATED_TO edges",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to connect with RELATED_TO edges",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="Auto-link free notes to the single active task in the current project",
    ),
    assistant: bool = typer.Option(
        False,
        "--assistant",
        "--agent",
        help="Mark task note as assistant-authored",
    ),
    author: str | None = typer.Option(None, "--author", "-a", help="Task note author"),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until free notes are persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add a task note or capture a free note memory."""
    task_note = _looks_like_task_id(subject)

    try:
        resolved_content = (
            resolve_content_input(
                content if content is not None else (None if task_note else subject),
                content_file=content_file,
                max_size=max_size,
                follow_symlinks=follow_symlinks,
            )
            or ""
        ).strip()
    except ValueError as e:
        error(str(e))
        raise typer.Exit(code=1) from e

    if not resolved_content:
        error("Provide note content as an argument, via stdin, or with --content-file.")
        raise typer.Exit(code=1)

    @run_async
    async def run_note() -> None:
        try:
            async with get_client() as client:
                if task_note:
                    resolved_id = await resolve_id_prefix(client, subject, entity_type="task")
                    response = await client.create_note(
                        resolved_id,
                        resolved_content,
                        "agent" if assistant else "user",
                        author or "",
                    )
                    if json_output:
                        print_json(response)
                        return
                    note_id = response.get("id")
                    if note_id:
                        success(f"Note added: {note_id}")
                    elif response.get("success"):
                        success(f"Note added to task: {resolved_id}")
                    else:
                        error("Failed to add note")
                    return

                parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
                data = await _write_memory_capture(
                    client,
                    title=subject
                    if content is not None
                    else _derive_capture_title(resolved_content),
                    content=resolved_content,
                    kind=EntityType.NOTE.value,
                    domain=None,
                    tags=parsed_tags,
                    related_ids=_parse_csv_ids(related_to),
                    task_ids=_parse_csv_ids(task),
                    active_task=active_task,
                    effective_project=project
                    or (None if all_projects else resolve_project_from_cwd()),
                    capture_mode="remember",
                    surface="cli",
                    wait_searchable=wait_searchable,
                )
                if json_output:
                    print_json(data)
                    return
                _print_memory_capture_result(
                    title=subject
                    if content is not None
                    else _derive_capture_title(resolved_content),
                    kind=EntityType.NOTE.value,
                    data=data,
                    wait_searchable=wait_searchable,
                )
        except SibylClientError as e:
            _handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e

    run_note()


@synthesis_app.command("plan")
def synthesis_plan_command(
    goal: str = typer.Argument(..., help="Synthesis goal"),
    output_type: str = typer.Option("documentation", "--type", help="Output type"),
    audience: str | None = typer.Option(None, "--audience", help="Intended audience"),
    depth: str = typer.Option("standard", "--depth", help="brief, standard, or deep"),
    seed_query: str | None = typer.Option(None, "--seed", help="Search seed query"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all-projects", help="Skip cwd project scope"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    entity_ids: str | None = typer.Option(None, "--entity", help="Comma-separated entity IDs"),
    decision_ids: str | None = typer.Option(
        None, "--decision", help="Comma-separated decision IDs"
    ),
    task_ids: str | None = typer.Option(None, "--task", help="Comma-separated task IDs"),
    artifact_ids: str | None = typer.Option(
        None, "--artifact", help="Comma-separated artifact IDs"
    ),
    sections: str | None = typer.Option(
        None,
        "--section",
        help="Pipe-separated Title::Prompt::source-id specs",
    ),
    constraints: str | None = typer.Option(
        None, "--constraint", help="Comma-separated constraints"
    ),
    max_sections: int = typer.Option(6, "--max-sections", min=1, max=12),
    include_neighborhoods: bool = typer.Option(
        True,
        "--neighborhoods/--no-neighborhoods",
        help="Include one-hop graph neighborhoods",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
) -> None:
    """Plan source-grounded synthesis from authorized memory."""
    options = _synthesis_options(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        all_projects=all_projects,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        sections=sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )

    @run_async
    async def run_synthesis_plan() -> None:
        try:
            async with get_client() as client:
                data = await client.synthesis_plan(**options)
            if json_output:
                print_json(data)
                return
            _print_synthesis_plan(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_synthesis_plan()


@synthesis_app.command("draft")
def synthesis_draft_command(
    goal: str = typer.Argument(..., help="Synthesis goal"),
    output_type: str = typer.Option("documentation", "--type", help="Output type"),
    output_format: str = typer.Option("markdown", "--format", help="markdown or json"),
    audience: str | None = typer.Option(None, "--audience", help="Intended audience"),
    depth: str = typer.Option("standard", "--depth", help="brief, standard, or deep"),
    seed_query: str | None = typer.Option(None, "--seed", help="Search seed query"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all-projects", help="Skip cwd project scope"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    entity_ids: str | None = typer.Option(None, "--entity", help="Comma-separated entity IDs"),
    decision_ids: str | None = typer.Option(
        None, "--decision", help="Comma-separated decision IDs"
    ),
    task_ids: str | None = typer.Option(None, "--task", help="Comma-separated task IDs"),
    artifact_ids: str | None = typer.Option(
        None, "--artifact", help="Comma-separated artifact IDs"
    ),
    sections: str | None = typer.Option(
        None,
        "--section",
        help="Pipe-separated Title::Prompt::source-id specs",
    ),
    constraints: str | None = typer.Option(
        None, "--constraint", help="Comma-separated constraints"
    ),
    max_sections: int = typer.Option(6, "--max-sections", min=1, max=12),
    include_neighborhoods: bool = typer.Option(
        True,
        "--neighborhoods/--no-neighborhoods",
        help="Include one-hop graph neighborhoods",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
) -> None:
    """Draft a verified synthesis artifact."""
    options = _synthesis_options(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        all_projects=all_projects,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        sections=sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )

    @run_async
    async def run_synthesis_draft() -> None:
        try:
            async with get_client() as client:
                data = await client.synthesis_draft(
                    **options,
                    output_format=output_format,
                )
            if json_output:
                print_json(data)
                return
            _print_synthesis_artifact(
                cast("dict[str, object]", data),
                output_format=output_format,
            )
        except SibylClientError as e:
            _handle_client_error(e)

    run_synthesis_draft()


@synthesis_app.command("verify")
def synthesis_verify_command(
    goal: str = typer.Argument(..., help="Synthesis goal"),
    output_type: str = typer.Option("documentation", "--type", help="Output type"),
    audience: str | None = typer.Option(None, "--audience", help="Intended audience"),
    depth: str = typer.Option("standard", "--depth", help="brief, standard, or deep"),
    seed_query: str | None = typer.Option(None, "--seed", help="Search seed query"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all-projects", help="Skip cwd project scope"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    entity_ids: str | None = typer.Option(None, "--entity", help="Comma-separated entity IDs"),
    decision_ids: str | None = typer.Option(
        None, "--decision", help="Comma-separated decision IDs"
    ),
    task_ids: str | None = typer.Option(None, "--task", help="Comma-separated task IDs"),
    artifact_ids: str | None = typer.Option(
        None, "--artifact", help="Comma-separated artifact IDs"
    ),
    sections: str | None = typer.Option(
        None,
        "--section",
        help="Pipe-separated Title::Prompt::source-id specs",
    ),
    constraints: str | None = typer.Option(
        None, "--constraint", help="Comma-separated constraints"
    ),
    max_sections: int = typer.Option(6, "--max-sections", min=1, max=12),
    include_neighborhoods: bool = typer.Option(
        True,
        "--neighborhoods/--no-neighborhoods",
        help="Include one-hop graph neighborhoods",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
) -> None:
    """Verify synthesis citation, freshness, redaction, and gap coverage."""
    options = _synthesis_options(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        all_projects=all_projects,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        sections=sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )

    @run_async
    async def run_synthesis_verify() -> None:
        try:
            async with get_client() as client:
                data = await client.synthesis_draft(**options, output_format="json")
            if json_output:
                print_json(data)
                return
            _print_synthesis_verification(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_synthesis_verify()


@synthesis_app.command("remember")
def synthesis_remember_command(
    goal: str = typer.Argument(..., help="Synthesis goal"),
    output_type: str = typer.Option("documentation", "--type", help="Output type"),
    output_format: str = typer.Option("markdown", "--format", help="markdown or json"),
    audience: str | None = typer.Option(None, "--audience", help="Intended audience"),
    depth: str = typer.Option("standard", "--depth", help="brief, standard, or deep"),
    seed_query: str | None = typer.Option(None, "--seed", help="Search seed query"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all-projects", help="Skip cwd project scope"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    entity_ids: str | None = typer.Option(None, "--entity", help="Comma-separated entity IDs"),
    decision_ids: str | None = typer.Option(
        None, "--decision", help="Comma-separated decision IDs"
    ),
    task_ids: str | None = typer.Option(None, "--task", help="Comma-separated task IDs"),
    artifact_ids: str | None = typer.Option(
        None, "--artifact", help="Comma-separated artifact IDs"
    ),
    sections: str | None = typer.Option(
        None,
        "--section",
        help="Pipe-separated Title::Prompt::source-id specs",
    ),
    constraints: str | None = typer.Option(
        None, "--constraint", help="Comma-separated constraints"
    ),
    max_sections: int = typer.Option(6, "--max-sections", min=1, max=12),
    include_neighborhoods: bool = typer.Option(
        True,
        "--neighborhoods/--no-neighborhoods",
        help="Include one-hop graph neighborhoods",
    ),
    memory_scope: str = typer.Option("private", "--scope", help="Artifact memory scope"),
    scope_key: str | None = typer.Option(None, "--scope-key", help="Artifact scope key"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated artifact tags"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
) -> None:
    """Draft, verify, and remember a synthesis artifact."""
    options = _synthesis_options(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        all_projects=all_projects,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        sections=sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
    )

    @run_async
    async def run_synthesis_remember() -> None:
        try:
            async with get_client() as client:
                data = await client.synthesis_draft(
                    **options,
                    output_format=output_format,
                    remember=True,
                    memory_scope=memory_scope,
                    scope_key=scope_key,
                    tags=_parse_csv_ids(tags),
                )
            if json_output:
                print_json(data)
                return
            _print_synthesis_remember(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_synthesis_remember()


@app.command("brief")
def brief_context(
    goal: str = typer.Argument(..., help="Subagent goal or task"),
    intent: str = typer.Option(
        "build",
        "--intent",
        "-i",
        callback=_normalize_context_intent,
        help=CONTEXT_INTENT_HELP,
    ),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Use all accessible projects"),
    budget: int = typer.Option(
        1500,
        "--budget",
        min=100,
        max=8000,
        help="Token budget for the rendered brief",
    ),
) -> None:
    """One-shot lean context brief for injecting into a subagent prompt.

    Prints wake-layer markdown only: no skill ceremony, no related-graph
    expansion, no JSON envelope. Pipe or paste straight into a worker
    agent's prompt.
    """
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_brief() -> None:
        try:
            async with get_client() as client:
                pack = await client.context_pack(
                    goal=goal,
                    intent=intent,
                    layer="wake",
                    project=effective_project,
                    limit=8,
                    include_related=False,
                    related_limit=0,
                    markdown_token_budget=budget,
                )
            sys.stdout.write((pack.get("markdown") or "") + "\n")
        except SibylClientError as e:
            _handle_client_error(e)

    run_brief()


@app.command("recall")
def recall_context(
    goal: str = typer.Argument(..., help="Agent goal or user task"),
    intent: str = typer.Option(
        "build",
        "--intent",
        "-i",
        callback=_normalize_context_intent,
        help=CONTEXT_INTENT_HELP,
    ),
    layer: str = typer.Option(
        "recall",
        "--layer",
        help="Context depth: wake, recall, deep_search",
    ),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    agent: str | None = typer.Option(None, "--agent", help="Agent diary identity to include"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Use all accessible projects"),
    limit: int = typer.Option(12, "--limit", "-l", min=1, max=50, help="Maximum context items"),
    related: bool = typer.Option(
        True,
        "--related/--no-related",
        help="Include one-hop related graph context",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
    audit: bool = typer.Option(
        False,
        "--audit",
        help="Include full retrieval metadata per item (for auditing noisy packs)",
    ),
    budget: int | None = typer.Option(
        None,
        "--budget",
        min=100,
        max=8000,
        help="Cap rendered markdown at roughly this many tokens",
    ),
    raw: bool = typer.Option(False, "--raw", help="Recall verbatim raw memories"),
    diary: bool = typer.Option(False, "--diary", help="Recall a private agent diary"),
    memory_scope: str = typer.Option("private", "--scope", help="Raw memory scope"),
    scope_key: str | None = typer.Option(None, "--scope-key", help="Project/team/shared scope key"),
    participant: Annotated[
        list[str] | None,
        typer.Option("--participant", help="Filter raw imports by participant"),
    ] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Filter raw imports by adapter label"),
    ] = None,
    thread_id: str | None = typer.Option(None, "--thread", help="Filter raw imports by thread"),
    occurred_after: str | None = typer.Option(
        None,
        "--occurred-after",
        help="Filter raw imports after an ISO timestamp",
    ),
    occurred_before: str | None = typer.Option(
        None,
        "--occurred-before",
        help="Filter raw imports before an ISO timestamp",
    ),
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help="Filter raw memory by validity timestamp",
    ),
) -> None:
    """Recall a compact working context pack for an agent."""
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_recall() -> None:
        try:
            async with get_client() as client:
                if diary and not agent:
                    error("Provide --agent when using --diary.")
                    raise typer.Exit(code=1)
                if raw or diary:
                    recall_kwargs: dict[str, Any] = {}
                    if participant:
                        recall_kwargs["participants"] = participant
                    if label:
                        recall_kwargs["labels"] = label
                    if thread_id:
                        recall_kwargs["thread_id"] = thread_id
                    if occurred_after:
                        recall_kwargs["occurred_after"] = occurred_after
                    if occurred_before:
                        recall_kwargs["occurred_before"] = occurred_before
                    if as_of:
                        recall_kwargs["as_of"] = as_of
                    data = await client.recall_raw_memory(
                        query=goal,
                        memory_scope=memory_scope,
                        scope_key=scope_key,
                        diary=diary,
                        agent_id=agent if diary else None,
                        project_id=effective_project if diary else None,
                        limit=limit,
                        **recall_kwargs,
                    )
                    if json_output:
                        print_json(data)
                        return
                    memories = data.get("memories", [])
                    _print_raw_memory_results(memories if isinstance(memories, list) else [])
                    return

                pack = await client.context_pack(
                    goal=goal,
                    intent=intent,
                    layer=layer,
                    domain=domain,
                    project=effective_project,
                    agent_id=agent,
                    limit=limit,
                    include_related=related,
                    related_limit=3,
                    audit=audit,
                    markdown_token_budget=budget,
                )

            if json_output:
                print_json(pack)
                return
            console.print(pack.get("markdown") or "")
        except SibylClientError as e:
            _handle_client_error(e)

    run_recall()


@app.command("memory-audit")
def memory_audit(
    action: str | None = typer.Option(None, "--action", "-a", help="Filter by audit action"),
    actor: str | None = typer.Option(None, "--actor", help="Filter by actor user ID"),
    source_id: str | None = typer.Option(None, "--source-id", help="Filter by source ID"),
    derived_id: str | None = typer.Option(None, "--derived-id", help="Filter by derived ID"),
    memory_scope: str | None = typer.Option(None, "--scope", help="Filter by memory scope"),
    project_id: str | None = typer.Option(None, "--project", "-p", help="Filter by project ID"),
    policy: str | None = typer.Option(None, "--policy", help="Filter: allowed or denied"),
    limit: int = typer.Option(50, "--limit", "-l", min=1, max=200, help="Maximum events"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Inspect memory audit receipts."""
    policy_allowed = _parse_policy_filter(policy)

    @run_async
    async def run_memory_audit() -> None:
        try:
            async with get_client() as client:
                data = await client.memory_audit(
                    action=action,
                    actor_user_id=actor,
                    source_id=source_id,
                    derived_id=derived_id,
                    memory_scope=memory_scope,
                    project_id=project_id,
                    policy_allowed=policy_allowed,
                    limit=limit,
                )
            if json_output:
                print_json(data)
                return
            events = data.get("events", [])
            _print_memory_audit_events(events if isinstance(events, list) else [])
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_audit()


@app.command("cite")
def cite_memories(
    cited_ids: Annotated[
        list[str],
        typer.Argument(
            help="Context/search item IDs that materially informed the answer",
        ),
    ],
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID for citation"),
    all_projects: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Do not attach the current directory project",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    misled: bool = typer.Option(
        False,
        "--misled",
        help="Mark these memories as having materially led the answer astray",
    ),
) -> None:
    """Record positive citation or negative misleading usage feedback."""
    parsed_ids = _parse_id_args(cited_ids)
    if not parsed_ids:
        error("Provide at least one cited memory ID")
        raise typer.Exit(1)
    effective_project = None if all_projects else project or resolve_project_from_cwd()

    @run_async
    async def run_cite_memories() -> None:
        try:
            async with get_client() as client:
                data = await client.cite_memory(
                    parsed_ids,
                    project_id=effective_project,
                    source_surface="cli_cite_misled" if misled else "cli_cite",
                    metadata={"command": "sibyl cite", "misled": misled},
                    misled=misled,
                )
            if json_output:
                print_json(data)
                return
            usage = data.get("usage", {})
            cited_count = usage.get("cited_count", len(parsed_ids))
            stamped_count = usage.get("stamped_count", 0)
            excluded_count = usage.get("excluded_count", 0)
            label = "misleading" if misled else "cited"
            success(f"Recorded {stamped_count}/{cited_count} {label} memories")
            if excluded_count:
                info(f"{excluded_count} citation(s) were accounted as exclusions")
        except SibylClientError as e:
            _handle_client_error(e)

    run_cite_memories()


@app.command("memory-inspect")
def memory_inspect(
    source_id: str = typer.Argument(..., help="Raw memory source ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Inspect a memory source and its audit trail."""

    @run_async
    async def run_memory_inspect() -> None:
        try:
            async with get_client() as client:
                data = await inspect_raw_memory_source(client, source_id)
            if json_output:
                print_json(data)
                return
            print_memory_source_inspect(data)
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_inspect()


@app.command("blame")
def blame_memory(
    source_id: str = typer.Argument(..., help="Raw memory source ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Show a memory's revisions, corrections, audits, and lineage."""

    @run_async
    async def run_blame_memory() -> None:
        try:
            async with get_client() as client:
                resolved_source_id = await resolve_raw_memory_id_prefix(
                    client,
                    raw_memory_lookup_value(source_id),
                )
                data = await client.memory_blame(resolved_source_id)
            if json_output:
                print_json(data)
                return
            print_memory_source_blame(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_blame_memory()


@app.command("correct")
def correct_memory(
    source_id: str = typer.Argument(..., help="Raw memory source ID"),
    action: str = typer.Option(
        ...,
        "--action",
        help="Correction: wrong, stale, duplicate, superseded, or revise",
    ),
    reason: str = typer.Option(..., "--reason", help="Why this correction is needed"),
    replacement: str | None = typer.Option(
        None,
        "--replacement",
        help="Replacement raw memory ID for --action superseded",
    ),
    duplicate_of: str | None = typer.Option(
        None,
        "--duplicate-of",
        help="Canonical raw memory ID for --action duplicate",
    ),
    content: str | None = typer.Option(None, "--content", help="Canonical body for revise"),
    content_file: str | None = typer.Option(
        None,
        "--content-file",
        help="Read revised canonical body from file",
    ),
    max_size: int = typer.Option(
        1_048_576,
        "--max-size",
        min=1,
        help="Maximum revised content size in bytes",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Allow --content-file to read through symlinks",
    ),
    expected_revision: int | None = typer.Option(
        None,
        "--expected-revision",
        min=1,
        help="Apply only if the memory still has this revision",
    ),
    preview: bool = typer.Option(False, "--preview", help="Validate without writing"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Correct or revise a raw memory with a durable receipt."""
    action_map = {
        "wrong": "mark_wrong",
        "stale": "mark_stale",
        "duplicate": "mark_duplicate",
        "superseded": "supersede",
        "revise": "revise",
    }
    normalized_action = action.strip().lower()
    api_action = action_map.get(normalized_action)
    if api_action is None:
        error("--action must be wrong, stale, duplicate, superseded, or revise")
        raise typer.Exit(code=1)
    reason = reason.strip()
    if not reason:
        error("--reason must not be empty")
        raise typer.Exit(code=1)
    if normalized_action == "duplicate" and not duplicate_of:
        error("--action duplicate requires --duplicate-of")
        raise typer.Exit(code=1)
    if normalized_action == "superseded" and not replacement:
        error("--action superseded requires --replacement")
        raise typer.Exit(code=1)
    if normalized_action != "revise" and (content is not None or content_file is not None):
        error("--content and --content-file are only valid with --action revise")
        raise typer.Exit(code=1)
    revised_content: str | None = None
    if normalized_action == "revise":
        try:
            revised_content = resolve_content_input(
                content,
                content_file=content_file,
                max_size=max_size,
                follow_symlinks=follow_symlinks,
            )
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e
        if revised_content is None or not revised_content.strip():
            error("--action revise requires --content, --content-file, or stdin")
            raise typer.Exit(code=1)

    @run_async
    async def run_correct_memory() -> None:
        try:
            async with get_client() as client:
                resolved_source_id = await resolve_raw_memory_id_prefix(
                    client,
                    raw_memory_lookup_value(source_id),
                )
                resolved_replacement = (
                    await resolve_raw_memory_id_prefix(
                        client,
                        raw_memory_lookup_value(replacement),
                    )
                    if replacement
                    else None
                )
                resolved_duplicate = (
                    await resolve_raw_memory_id_prefix(
                        client,
                        raw_memory_lookup_value(duplicate_of),
                    )
                    if duplicate_of
                    else None
                )
                data = await client.correct_memory(
                    resolved_source_id,
                    action=api_action,
                    reason=reason,
                    replacement_source_id=resolved_replacement,
                    duplicate_of_source_id=resolved_duplicate,
                    revised_content=revised_content,
                    expected_revision=expected_revision,
                    preview=preview,
                )
            applied = data.get("applied") is True
            allowed = data.get("allowed") is True
            if json_output:
                print_json(data)
                if applied or (preview and allowed):
                    return
                raise typer.Exit(code=1)
            if preview and allowed:
                success(f"Correction preview allowed: {normalized_action}")
                return
            if applied:
                success(f"Memory corrected: {normalized_action}")
                print_mutation_receipt(data)
                return
            error(f"Correction denied: {data.get('reason') or 'unknown reason'}")
            print_mutation_receipt(data)
            raise typer.Exit(code=1)
        except SibylClientError as e:
            _handle_client_error(e)

    run_correct_memory()


@app.command("memory-import-status")
def memory_import_status(
    import_id: str = typer.Argument(..., help="Source import ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Inspect a source import receipt and its published raw memory IDs."""

    @run_async
    async def run_memory_import_status() -> None:
        try:
            async with get_client() as client:
                data = await client.source_import_status(import_id)
            if json_output:
                print_json(data)
                return
            _print_source_import_status(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_import_status()


@app.command("memory-promote")
def memory_promote(
    candidate_id: str = typer.Argument(..., help="Raw memory or reflection candidate ID"),
    preview: bool = typer.Option(False, "--preview", help="Preview without promoting"),
    apply_changes: bool = typer.Option(False, "--apply", help="Apply the promotion"),
    auto: bool = typer.Option(False, "--auto", help="Auto-review and promote when safe"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Evaluate auto-review without applying"),
    confidence_threshold: float | None = typer.Option(
        None,
        "--confidence-threshold",
        min=0.0,
        max=1.0,
        help="Override the auto-review confidence threshold",
    ),
    promote_to_scope: str | None = typer.Option(None, "--scope", help="Target memory scope"),
    promote_to_scope_key: str | None = typer.Option(None, "--scope-key", help="Target scope key"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help="Comma-separated graph IDs to relate after promotion",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to relate after promotion",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Preview or apply memory promotion."""
    selected_modes = sum(1 for selected in (preview, apply_changes, auto) if selected)
    if selected_modes > 1:
        error("Choose only one of --preview, --apply, or --auto.")
        raise typer.Exit(code=1)
    if dry_run and not auto:
        error("--dry-run is only available with --auto.")
        raise typer.Exit(code=1)
    if confidence_threshold is not None and not auto:
        error("--confidence-threshold is only available with --auto.")
        raise typer.Exit(code=1)
    if selected_modes == 0:
        error("memory-promote requires --preview, --apply, or --auto.")
        raise typer.Exit(code=1)

    effective_project = project or (None if all_projects else resolve_project_from_cwd())
    target_scope_key = promote_to_scope_key
    if promote_to_scope == "project" and target_scope_key is None:
        target_scope_key = effective_project
    related_ids = _append_unique_ids(_parse_csv_ids(related_to), _parse_csv_ids(task))

    @run_async
    async def run_memory_promote() -> None:
        try:
            async with get_client() as client:
                resolved_candidate_id = await resolve_raw_memory_id_prefix(client, candidate_id)
                if auto:
                    data = await client.auto_review_reflection_promotion(
                        candidate_id=resolved_candidate_id,
                        promote_to_scope=promote_to_scope,
                        promote_to_scope_key=target_scope_key,
                        domain=domain,
                        project=effective_project,
                        related_to=related_ids,
                        dry_run=dry_run,
                        confidence_threshold=confidence_threshold,
                    )
                elif apply_changes:
                    data = await client.promote_memory(
                        candidate_id=resolved_candidate_id,
                        promote_to_scope=promote_to_scope,
                        promote_to_scope_key=target_scope_key,
                        domain=domain,
                        project=effective_project,
                        related_to=related_ids,
                    )
                else:
                    data = await client.preview_memory_promotion(
                        candidate_id=resolved_candidate_id,
                        promote_to_scope=promote_to_scope,
                        promote_to_scope_key=target_scope_key,
                        domain=domain,
                        project=effective_project,
                        related_to=related_ids,
                    )
            if json_output:
                print_json(data)
                return
            payload = cast("dict[str, object]", data)
            if auto:
                _print_promotion_autonomy(payload)
            elif apply_changes:
                _print_promotion_result(payload)
            else:
                _print_promotion_preview(payload)
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_promote()


@memory_review_app.command("drain")
def memory_review_drain(
    apply_changes: bool = typer.Option(
        False,
        "--apply",
        help="Apply safe promotions instead of only previewing the drain",
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="Candidates to process"),
    confidence_threshold: float | None = typer.Option(
        None,
        "--confidence-threshold",
        min=0.0,
        max=1.0,
        help="Override the auto-review confidence threshold",
    ),
    promote_to_scope: str | None = typer.Option(None, "--scope", help="Target memory scope"),
    promote_to_scope_key: str | None = typer.Option(None, "--scope-key", help="Target scope key"),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help="Comma-separated graph IDs to relate after promotion",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to relate after promotion",
    ),
    archive_exceptions: bool = typer.Option(
        False,
        "--archive-exceptions",
        help="Archive terminal duplicate/stale exceptions when applying",
    ),
    archive_reasons: str = typer.Option(
        "duplicate_candidate,stale_candidate",
        "--archive-reasons",
        help="Comma-separated exception reasons eligible for archive",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Drain pending reflection candidates through automatic review."""
    effective_project = project or (None if all_projects else resolve_project_from_cwd())
    target_scope_key = promote_to_scope_key
    if promote_to_scope == "project" and target_scope_key is None:
        target_scope_key = effective_project
    related_ids = _append_unique_ids(_parse_csv_ids(related_to), _parse_csv_ids(task))
    archive_reason_ids = _parse_csv_ids(archive_reasons)

    @run_async
    async def run_memory_review_drain() -> None:
        try:
            async with get_client() as client:
                data = await client.drain_reflection_review(
                    dry_run=not apply_changes,
                    limit=limit,
                    promote_to_scope=promote_to_scope,
                    promote_to_scope_key=target_scope_key,
                    domain=domain,
                    project=effective_project,
                    related_to=related_ids,
                    confidence_threshold=confidence_threshold,
                    archive_exceptions=archive_exceptions,
                    archive_exception_reasons=archive_reason_ids,
                )
            if json_output:
                print_json(data)
                return
            _print_memory_review_drain(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_review_drain()


@memory_review_app.command("dream")
def memory_review_dream(
    apply_changes: bool = typer.Option(
        False,
        "--apply",
        help="Apply safe automatic promotions instead of queueing a dry run",
    ),
    source_limit: int = typer.Option(20, "--source-limit", min=0, max=100, help="Raw sources"),
    candidate_limit: int = typer.Option(
        50,
        "--candidate-limit",
        min=0,
        max=200,
        help="Pending reflection candidates",
    ),
    archive_exceptions: bool = typer.Option(
        True,
        "--archive-exceptions/--keep-exceptions",
        help="Archive terminal duplicate/stale exceptions when applying",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Queue the automatic reflection dream-cycle maintenance job."""
    dry_run = not apply_changes

    @run_async
    async def run_memory_review_dream() -> None:
        try:
            async with get_client() as client:
                data = await client.enqueue_reflection_dream_cycle(
                    dry_run=dry_run,
                    source_limit=source_limit,
                    candidate_limit=candidate_limit,
                    archive_exceptions=archive_exceptions,
                )
            if json_output:
                print_json(data)
                return
            _print_reflection_dream_enqueue(cast("dict[str, object]", data), dry_run=dry_run)
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_review_dream()


@memory_review_app.command("status")
def memory_review_status(
    limit: int = typer.Option(10, "--limit", "-l", min=1, max=50, help="Maximum runs/events"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Show reflection dream-cycle runs and automatic decision receipts."""

    @run_async
    async def run_memory_review_status() -> None:
        try:
            async with get_client() as client:
                jobs, promoted, reviewed = await asyncio.gather(
                    client.list_jobs(
                        function="run_reflection_dream_cycle",
                        limit=limit,
                    ),
                    client.memory_audit(
                        action="memory.reflect.dream_promote",
                        limit=limit,
                    ),
                    client.memory_audit(
                        action="memory.reflect.dream_review",
                        limit=limit,
                    ),
                )
            events = [
                *(promoted.get("events", []) if isinstance(promoted.get("events"), list) else []),
                *(reviewed.get("events", []) if isinstance(reviewed.get("events"), list) else []),
            ]
            events = sorted(
                (event for event in events if isinstance(event, dict)),
                key=lambda event: str(cast("dict[str, object]", event).get("created_at") or ""),
                reverse=True,
            )[:limit]
            payload = {
                "jobs": jobs.get("jobs", []) if isinstance(jobs.get("jobs"), list) else [],
                "events": events,
            }
            if json_output:
                print_json(payload)
                return
            _print_reflection_dream_status(payload)
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_review_status()


@app.command("memory-share")
def memory_share(
    source_ids: Annotated[
        list[str],
        typer.Argument(help="Raw memory IDs to share-preview"),
    ],
    apply: bool = typer.Option(False, "--apply", help="Apply sharing writes"),
    preview: bool = typer.Option(False, "--preview", help="Preview without sharing"),
    target_scope: str | None = typer.Option(None, "--target-scope", help="Intended target scope"),
    target_scope_key: str | None = typer.Option(None, "--target-key", help="Target scope key"),
    recipient_organization_id: str | None = typer.Option(
        None,
        "--recipient-org",
        help="Future recipient organization ID",
    ),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Preview or apply promotion-backed memory sharing."""
    if not target_scope:
        error("Provide --target-scope for memory share.")
        raise typer.Exit(code=1)
    if apply and preview:
        error("Use either --apply or --preview, not both.")
        raise typer.Exit(code=1)

    parsed_source_ids = _parse_id_args(source_ids)
    if not parsed_source_ids:
        error("Provide at least one raw memory ID.")
        raise typer.Exit(code=1)

    effective_project = project
    if target_scope == "project" and effective_project is None and not all_projects:
        effective_project = resolve_project_from_cwd()
    resolved_target_key = target_scope_key
    if target_scope == "project" and resolved_target_key is None:
        resolved_target_key = effective_project
    project_id = resolved_target_key if target_scope == "project" else project

    @run_async
    async def run_memory_share() -> None:
        try:
            async with get_client() as client:
                resolved_source_ids = [
                    await resolve_raw_memory_id_prefix(client, source_id)
                    for source_id in parsed_source_ids
                ]
                if not apply:
                    data = await client.preview_memory_share(
                        source_ids=resolved_source_ids,
                        target_scope=target_scope,
                        target_scope_key=resolved_target_key,
                        recipient_organization_id=recipient_organization_id,
                        project_id=project_id,
                    )
                else:
                    data = await client.share_memory(
                        source_ids=resolved_source_ids,
                        target_scope=target_scope,
                        target_scope_key=resolved_target_key,
                        recipient_organization_id=recipient_organization_id,
                        project_id=project_id,
                    )
            if json_output:
                print_json(data)
                return
            if not apply:
                _print_share_preview(cast("dict[str, object]", data))
            else:
                _print_share_result(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_share()


@memory_space_app.command("preview-agent")
def memory_space_preview_agent(
    agent_id: str = typer.Argument(..., help="Agent principal ID"),
    space_id: str = typer.Option(..., "--space", help="Primary memory space ID"),
    additional_spaces: str | None = typer.Option(
        None,
        "--also-space",
        help="Comma-separated additional memory space IDs",
    ),
    limit: int = typer.Option(50, "--limit", "-l", min=1, max=200, help="Maximum sources"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Preview what an agent could recall from selected memory spaces."""
    extra_space_ids = _parse_csv_ids(additional_spaces)

    @run_async
    async def run_memory_space_preview_agent() -> None:
        try:
            async with get_client() as client:
                data = await client.preview_memory_space_access(
                    space_id=space_id,
                    target_principal_type="agent",
                    target_principal_id=agent_id,
                    additional_space_ids=extra_space_ids,
                    limit=limit,
                )
            if json_output:
                print_json(data)
                return
            _print_access_preview(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_space_preview_agent()


@team_app.command("list")
def list_teams_cmd(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """List teams in the active organization."""

    @run_async
    async def run_list_teams() -> None:
        try:
            async with get_client() as client:
                data = await client.list_teams()
            if json_output:
                print_json(data)
                return
            _print_team_list(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_list_teams()


@team_app.command("create")
def create_team_cmd(
    name: str = typer.Argument(..., help="Team name"),
    slug: str | None = typer.Option(None, "--slug", help="Stable team slug"),
    description: str | None = typer.Option(None, "--description", "-d", help="Team description"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Create a team and its team memory space."""

    @run_async
    async def run_create_team() -> None:
        try:
            async with get_client() as client:
                data = await client.create_team(
                    name=name,
                    slug=slug,
                    description=description,
                )
            if json_output:
                print_json(data)
                return
            _print_team(cast("dict[str, object]", data))
            success(f"Created team: {data.get('slug') or data.get('id')}")
        except SibylClientError as e:
            _handle_client_error(e)

    run_create_team()


@team_app.command("add-member")
def add_team_member_cmd(
    team_id: str = typer.Argument(..., help="Team ID or slug"),
    user_id: str = typer.Argument(..., help="User UUID"),
    role: str = typer.Option("member", "--role", "-r", help="Team role"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add or update a team member."""

    @run_async
    async def run_add_team_member() -> None:
        try:
            async with get_client() as client:
                data = await client.add_team_member(
                    team_id=team_id,
                    user_id=user_id,
                    role=role,
                )
            if json_output:
                print_json(data)
                return
            success(f"Added {data.get('user_id')} to team {team_id} as {data.get('role')}")
        except SibylClientError as e:
            _handle_client_error(e)

    run_add_team_member()


@team_app.command("remove-member")
def remove_team_member_cmd(
    team_id: str = typer.Argument(..., help="Team ID or slug"),
    user_id: str = typer.Argument(..., help="User UUID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Remove a team member."""

    @run_async
    async def run_remove_team_member() -> None:
        try:
            async with get_client() as client:
                data = await client.remove_team_member(team_id=team_id, user_id=user_id)
            if json_output:
                print_json(data)
                return
            success(f"Removed {user_id} from team {team_id}")
        except SibylClientError as e:
            _handle_client_error(e)

    run_remove_team_member()


@team_app.command("link-project")
def link_team_project_cmd(
    team_id: str = typer.Argument(..., help="Team ID or slug"),
    project_id: str = typer.Argument(..., help="Project UUID or graph project ID"),
    role: str = typer.Option(
        "project_contributor",
        "--role",
        "-r",
        help="Project role granted to the team",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Grant a team access to a project."""

    @run_async
    async def run_link_team_project() -> None:
        try:
            async with get_client() as client:
                data = await client.link_team_project(
                    team_id=team_id,
                    project_id=project_id,
                    role=role,
                )
            if json_output:
                print_json(data)
                return
            success(
                f"Linked team {team_id} to project "
                f"{data.get('graph_project_id') or data.get('project_id')}"
            )
        except SibylClientError as e:
            _handle_client_error(e)

    run_link_team_project()


@team_app.command("unlink-project")
def unlink_team_project_cmd(
    team_id: str = typer.Argument(..., help="Team ID or slug"),
    project_id: str = typer.Argument(..., help="Project UUID or graph project ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Remove a team's project access."""

    @run_async
    async def run_unlink_team_project() -> None:
        try:
            async with get_client() as client:
                data = await client.unlink_team_project(team_id=team_id, project_id=project_id)
            if json_output:
                print_json(data)
                return
            success(f"Unlinked team {team_id} from project {project_id}")
        except SibylClientError as e:
            _handle_client_error(e)

    run_unlink_team_project()


@app.command("remember")
def remember_memory(
    title: str = typer.Argument(..., help="Title/name of the memory"),
    content: str | None = typer.Argument(
        None,
        help="Memory body. Reads stdin if omitted.",
    ),
    content_option: str | None = typer.Option(None, "--content", help="Memory body"),
    content_file: str | None = typer.Option(None, "--content-file", help="Read content from file"),
    max_size: int = typer.Option(
        1_048_576,
        "--max-size",
        min=1,
        help="Maximum content file size in bytes",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Allow --content-file to read through symlinks",
    ),
    kind: str = typer.Option(
        "episode",
        "--kind",
        "-k",
        callback=_normalize_memory_kind,
        help=ENTITY_TYPE_HELP,
    ),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help="Comma-separated entity IDs to connect with RELATED_TO edges",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to connect with RELATED_TO edges",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="Auto-link to the single active task in the current project",
    ),
    surface: str = typer.Option("cli", "--surface", help="Capture surface metadata"),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new memory is persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    raw: bool = typer.Option(False, "--raw", help="Store verbatim raw memory only"),
    diary: bool = typer.Option(False, "--diary", help="Store a private agent diary entry"),
    agent: str | None = typer.Option(None, "--agent", help="Agent identity for diary entries"),
    source_id: str | None = typer.Option(None, "--source-id", help="Raw memory source ID"),
    memory_scope: str = typer.Option("private", "--scope", help="Raw memory scope"),
    scope_key: str | None = typer.Option(None, "--scope-key", help="Project/team/shared scope key"),
    pin: bool = typer.Option(False, "--pin", help="Exempt this memory from ordinary decay"),
    basis: str | None = typer.Option(
        None,
        "--basis",
        callback=_normalize_memory_basis,
        help="Epistemic basis: observed, inferred, told, or assumed",
    ),
    propose_scope: str | None = typer.Option(
        None,
        "--propose-scope",
        callback=_normalize_memory_proposal_scope,
        help="Nominate this memory for audited promotion to team scope",
    ),
) -> None:
    """Remember a decision, plan, idea, claim, artifact, session, or learning."""

    try:
        resolved_content = (
            resolve_content_input(
                content_option if content_option is not None else content,
                content_file=content_file,
                max_size=max_size,
                follow_symlinks=follow_symlinks,
            )
            or ""
        ).strip()
    except ValueError as e:
        error(str(e))
        raise typer.Exit(code=1) from e
    if not resolved_content:
        error("Provide memory content as an argument, via stdin, or with --content-file.")
        raise typer.Exit(code=1)

    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    related_ids = _parse_csv_ids(related_to)
    task_ids = _parse_csv_ids(task)
    metadata = {
        "capture_mode": "remember",
        "capture_surface": surface,
        "remember_kind": kind,
    }
    capture_metadata: dict[str, Any] = {}
    if pin:
        capture_metadata["pinned"] = True
    if basis:
        capture_metadata["basis"] = basis
    if propose_scope:
        capture_metadata["suggested_memory_scope"] = propose_scope
    metadata.update(capture_metadata)
    if domain:
        metadata["domain"] = domain

    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_remember() -> None:
        try:
            async with get_client() as client:
                if diary and not agent:
                    error("Provide --agent when using --diary.")
                    raise typer.Exit(code=1)
                if raw or diary:
                    resolved_project = (
                        await resolve_project_reference(client, effective_project)
                        if effective_project
                        else None
                    )
                    if resolved_project:
                        metadata["project_id"] = resolved_project
                    data = await client.remember_raw_memory(
                        title=title,
                        raw_content=resolved_content,
                        source_id=source_id,
                        memory_scope=memory_scope,
                        scope_key=scope_key,
                        diary=diary,
                        agent_id=agent,
                        project_id=resolved_project if diary else None,
                        tags=parsed_tags,
                        metadata=metadata,
                        provenance={"remember_kind": kind},
                        capture_surface=surface,
                    )

                    memory_id = data.get("id", "unknown")
                    if json_output:
                        print_json(data)
                        return

                    label = f"diary entry for {agent}" if diary else "raw memory"
                    success(f"Remembered {label}: {title}")
                    print_mutation_receipt(data)
                    console.print(f"  [dim]ID: {memory_id}[/dim]")
                    if policy_reason := data.get("policy_reason"):
                        console.print(f"  [dim]Policy: {policy_reason}[/dim]")
                    return

                data = await _write_memory_capture(
                    client,
                    title=title,
                    content=resolved_content,
                    kind=kind,
                    domain=domain,
                    tags=parsed_tags,
                    related_ids=related_ids,
                    task_ids=task_ids,
                    active_task=active_task,
                    effective_project=effective_project,
                    capture_mode="remember",
                    surface=surface,
                    wait_searchable=wait_searchable,
                    memory_scope=memory_scope,
                    scope_key=scope_key,
                    source_id=source_id,
                    capture_metadata=capture_metadata,
                )

                if json_output:
                    print_json(data)
                    return

                _print_memory_capture_result(
                    title=title,
                    kind=kind,
                    data=data,
                    wait_searchable=wait_searchable,
                )
        except SibylClientError as e:
            _handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e

    run_remember()


@app.command("reflect")
def reflect_memory(
    content: str | None = typer.Argument(
        None,
        help="Raw notes to reflect. Reads stdin if omitted.",
    ),
    title: str = typer.Option("Session reflection", "--title", "-t", help="Source/session title"),
    intent: str = typer.Option(
        "general",
        "--intent",
        "-i",
        callback=_normalize_context_intent,
        help=f"Intent: {', '.join(CONTEXT_INTENT_VALUES)}",
    ),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help="Comma-separated entity IDs to link persisted candidates to",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to link persisted output to",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="When persisting, auto-link to the single active task in the current project",
    ),
    persist: bool = typer.Option(False, "--persist", help="Persist candidates into the graph"),
    persist_source: bool = typer.Option(
        True,
        "--source/--no-source",
        help="When persisting, also store the raw notes as a session memory",
    ),
    persist_review: bool = typer.Option(
        False,
        "--review",
        help="Store persisted output in the raw review queue instead of graph promotion",
    ),
    cited: str | None = typer.Option(
        None,
        "--cited",
        help="Comma-separated context/search IDs that informed this reflection",
    ),
    limit: int = typer.Option(12, "--limit", "-l", min=1, max=25, help="Maximum candidates"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Reflect raw notes into memory candidates, optionally persisting them."""

    resolved_content = content
    if resolved_content is None and not sys.stdin.isatty():
        resolved_content = sys.stdin.read()

    resolved_content = (resolved_content or "").strip()
    if not resolved_content:
        error("Provide notes as an argument or via stdin.")
        raise typer.Exit(code=1)

    effective_project = project or (None if all_projects else resolve_project_from_cwd())
    related_ids = _parse_csv_ids(related_to)
    task_ids = _parse_csv_ids(task)
    cited_ids = _parse_csv_ids(cited)

    @run_async
    async def run_reflect() -> None:
        try:
            async with get_client() as client:
                resolved_links = await _resolve_capture_links(
                    client=client,
                    project=effective_project,
                    related_ids=related_ids,
                    task_ids=task_ids,
                    active_task=active_task and persist,
                )
                data = await client.reflect(
                    content=resolved_content,
                    source_title=title,
                    intent=intent,
                    domain=domain,
                    project=effective_project,
                    related_to=resolved_links,
                    persist=persist,
                    persist_source=persist_source,
                    persist_review=persist_review,
                    cited_ids=cited_ids or None,
                    limit=limit,
                )

            if json_output:
                print_json(data)
                return

            console.print(data.get("markdown") or "")
            _print_reflection_persistence_summary(
                data,
                persist=persist,
                persist_source=persist_source,
            )
            citation_usage = data.get("citation_usage", {})
            if citation_usage:
                info(
                    "Citations recorded: "
                    f"{citation_usage.get('stamped_count', 0)}/"
                    f"{citation_usage.get('cited_count', len(cited_ids))}"
                )
        except SibylClientError as e:
            _handle_client_error(e)

    run_reflect()


@app.command()
def stats(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Show knowledge graph statistics."""

    @run_async
    async def get_stats() -> None:
        try:
            async with get_client() as client:
                data = await client.get("/admin/stats")

                if json_output:
                    print_json(data)
                    return

                console.print("\n[bold]Knowledge Graph Statistics[/bold]\n")

                if counts := data.get("entity_counts"):
                    table = create_table("Entity Type", "Count")
                    for etype, count in sorted(counts.items()):
                        table.add_row(etype, str(count))
                    console.print(table)
                    console.print()

                if rel_counts := data.get("relationship_counts"):
                    table = create_table("Relationship Type", "Count")
                    for rtype, count in sorted(rel_counts.items()):
                        table.add_row(rtype, str(count))
                    console.print(table)
                console.print()
        except SibylClientError as e:
            _handle_client_error(e)

    get_stats()


@app.command()
def version() -> None:
    """Show version information."""
    console.print(f"sibyl {get_version()}")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
