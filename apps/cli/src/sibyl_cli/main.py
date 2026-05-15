"""Main CLI application - client-side commands for Sibyl.

This is the entry point for the sibyl-dev package.
All commands communicate with the REST API.

Server commands (serve, dev, db, generate, etc.) are in sibyl-server.
"""

import asyncio
import re
import sys
from importlib.metadata import version as pkg_version
from os import environ
from typing import Annotated, Any, cast

import typer

from sibyl_cli.archive import app as archive_app
from sibyl_cli.auth import app as auth_app
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
    run_async,
    success,
)
from sibyl_cli.config_cmd import app as config_app
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.context import app as context_app
from sibyl_cli.crawl import app as crawl_app
from sibyl_cli.debug import app as debug_app
from sibyl_cli.dev import app as dev_app
from sibyl_cli.entity import app as entity_app
from sibyl_cli.epic import app as epic_app
from sibyl_cli.explore import app as explore_app
from sibyl_cli.local import app as local_app
from sibyl_cli.logs import app as logs_app
from sibyl_cli.org import app as org_app
from sibyl_cli.project import app as project_app
from sibyl_cli.project_refs import resolve_project_reference
from sibyl_cli.session import app as session_app
from sibyl_cli.skill import app as skill_app
from sibyl_cli.state import set_context_override
from sibyl_cli.task import app as task_app
from sibyl_cli.task import list_tasks
from sibyl_cli.update import app as update_app


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


# Register subcommand groups
app.add_typer(task_app, name="task")
app.add_typer(epic_app, name="epic")
app.add_typer(project_app, name="project")
app.add_typer(archive_app, name="archive")
app.add_typer(session_app, name="session")
app.add_typer(entity_app, name="entity")
app.add_typer(explore_app, name="explore")
app.add_typer(crawl_app, name="crawl")
app.add_typer(debug_app, name="debug")
app.add_typer(dev_app, name="dev")
app.add_typer(auth_app, name="auth")
app.add_typer(org_app, name="org")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")
app.add_typer(local_app, name="local")
app.add_typer(logs_app, name="logs")
app.add_typer(update_app, name="update")
app.add_typer(skill_app, name="skill")
app.add_typer(memory_space_app, name="memory-space")
app.add_typer(memory_review_app, name="memory-review")
app.add_typer(synthesis_app, name="synthesis")
app.command("tasks", hidden=True)(list_tasks)


SEARCH_PREVIEW_CHARS = 220
CAPTURE_TITLE_CHARS = 72
QUIET_ENV_VALUES = {"1", "true", "yes", "on"}


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
    if ctx.invoked_subcommand in {None, "health"}:
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
        score = memory.get("score")
        scope = str(memory.get("memory_scope") or "private")
        policy_reason = str(memory.get("policy_reason") or "")

        source_label = f" [dim]({source_id})[/dim]" if source_id else ""
        console.print(f"  [{NEON_CYAN}]{title}[/{NEON_CYAN}]{source_label}")
        if content:
            console.print(f"    {_format_search_preview(content)}", soft_wrap=True)
        score_label = f" score={score}" if score else ""
        policy_label = f" policy={policy_reason}" if policy_reason else ""
        console.print(f"    [dim]scope={scope}{score_label}{policy_label}[/dim]")
        console.print(f"    [{CORAL}]{memory_id}[/{CORAL}]")
        console.print()


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
        console.print(f"  [dim]Memory: {remembered_memory_id}[/dim]")
        console.print(f"  [dim]Source: {remembered_source_id}[/dim]")
        return
    error("Synthesis artifact was drafted but not remembered.")


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


def _print_memory_source_inspect(data: dict[str, object]) -> None:
    console.print("\n[bold]Memory source[/bold]\n")
    scope = str(data.get("memory_scope") or "")
    if scope_key := data.get("scope_key"):
        scope = f"{scope}:{scope_key}" if scope else str(scope_key)
    policy = _format_policy_state(data.get("policy_allowed"))
    if reason := data.get("policy_reason"):
        policy = f"{policy} ({reason})"
    content_state = "redacted" if data.get("content_redacted") else "visible"

    table = create_table(None, "Field", "Value", expand=False)
    table.add_row("ID", str(data.get("id") or ""))
    table.add_row("Source", str(data.get("source_id") or ""))
    table.add_row("Title", str(data.get("title") or ""))
    table.add_row("Scope", scope)
    table.add_row("Project", str(data.get("project_id") or ""))
    table.add_row("Review", str(data.get("review_state") or ""))
    promotion = data.get("promotion_state")
    if isinstance(promotion, dict):
        promotion_payload = cast("dict[str, object]", promotion)
        table.add_row("Promotion", str(promotion_payload.get("state") or ""))
    table.add_row("Corrections", _inspect_correction_count(data.get("correction_history")))
    table.add_row("Entity type", str(data.get("entity_type") or ""))
    table.add_row("Policy", policy)
    table.add_row("Content", content_state)
    table.add_row("Derived", _audit_id_summary(data.get("derived_ids")))
    table.add_row("Audits", str(data.get("audit_event_count") or 0))
    table.add_row("Actions", _inspect_action_summary(data.get("available_actions")))
    console.print(table)

    raw_content = data.get("raw_content")
    if isinstance(raw_content, str) and raw_content:
        console.print()
        console.print(_format_search_preview(raw_content), soft_wrap=True)


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


def _inspect_correction_count(value: object) -> str:
    if isinstance(value, list):
        return str(len(value))
    return "0"


def _inspect_action_summary(value: object) -> str:
    if not isinstance(value, list):
        return "-"
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        payload = cast("dict[str, object]", item)
        if payload.get("available") is True:
            names.append(str(payload.get("action")))
    return ", ".join(names) if names else "-"


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


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if "Cannot connect" in str(e):
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
            help="Override project context for this command (project ID or name)",
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


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    entity_type: str | None = typer.Option(None, "--type", "-t", help="Filter by entity type"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Search all projects"),
    graph_only: bool = typer.Option(False, "--graph-only", help="Search graph memory only"),
    docs_only: bool = typer.Option(False, "--docs-only", help="Search crawled docs only"),
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
                data = await client.search(
                    query,
                    types=types,
                    limit=limit,
                    project=effective_project,
                    include_documents=include_documents,
                    include_graph=include_graph,
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
                    origin_label = "docs" if origin == "document" else "graph"

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
                        console.print(f"    {_format_search_preview(content)}", soft_wrap=True)

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
                    hints.append(f"[{NEON_CYAN}]sibyl entity show <id>[/{NEON_CYAN}]")
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
    entity_type: str = typer.Option("episode", "--type", "-t", help="Entity type"),
    category: str | None = typer.Option(None, "--category", "-c", help="Category"),
    language: str | None = typer.Option(None, "--language", "-l", help="Language"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new entity is persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add knowledge to the graph."""
    resolved_title = (title_option or title or "").strip()
    resolved_content = (content_option or content or "").strip()
    if not resolved_title:
        error("Provide a title as an argument or with --title.")
        raise typer.Exit(code=1)
    if not resolved_content:
        error("Provide content as an argument or with --content.")
        raise typer.Exit(code=1)

    @run_async
    async def run_add() -> None:
        try:
            async with get_client() as client:
                data = await client.create_entity(
                    name=resolved_title,
                    content=resolved_content,
                    entity_type=entity_type,
                    category=category,
                    languages=[language] if language else None,
                    tags=[t.strip() for t in tags.split(",")] if tags else None,
                    sync=wait_searchable,
                )

                entity_id = data.get("id", "unknown")

                if json_output:
                    print_json(data)
                    return

                if wait_searchable:
                    success(f"Added {entity_type}: {resolved_title}")
                else:
                    info(f"Queued {entity_type}: {resolved_title}")
                console.print(f"  [dim]ID: {entity_id}[/dim]")
        except SibylClientError as e:
            _handle_client_error(e)

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
    entity_type: str = typer.Option("episode", "--type", help="Entity type for the capture"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new entity is persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Capture a quick memory without separate title and content fields."""

    resolved_content = content
    if resolved_content is None and not sys.stdin.isatty():
        resolved_content = sys.stdin.read()

    resolved_content = (resolved_content or "").strip()
    if not resolved_content:
        error("Provide capture content as an argument or via stdin.")
        raise typer.Exit(code=1)

    resolved_title = (title or "").strip() or _derive_capture_title(resolved_content)

    @run_async
    async def run_capture() -> None:
        try:
            async with get_client() as client:
                data = await client.create_entity(
                    name=resolved_title,
                    content=resolved_content,
                    entity_type=entity_type,
                    tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
                    metadata={"capture_mode": "quick", "capture_surface": "cli"},
                    sync=wait_searchable,
                )

                entity_id = data.get("id", "unknown")

                if json_output:
                    print_json(data)
                    return

                if wait_searchable:
                    success(f"Captured {entity_type}: {resolved_title}")
                else:
                    info(f"Queued {entity_type}: {resolved_title}")
                console.print(f"  [dim]ID: {entity_id}[/dim]")
        except SibylClientError as e:
            _handle_client_error(e)

    run_capture()


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


@app.command("recall")
def recall_context(
    goal: str = typer.Argument(..., help="Agent goal or user task"),
    intent: str = typer.Option(
        "build",
        "--intent",
        "-i",
        help="Agent intent: build, plan, ideate, research, debug, decide, learn, general",
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
    raw: bool = typer.Option(False, "--raw", help="Recall verbatim raw memories"),
    diary: bool = typer.Option(False, "--diary", help="Recall a private agent diary"),
    memory_scope: str = typer.Option("private", "--scope", help="Raw memory scope"),
    scope_key: str | None = typer.Option(None, "--scope-key", help="Project/team/shared scope key"),
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
                    data = await client.recall_raw_memory(
                        query=goal,
                        memory_scope=memory_scope,
                        scope_key=scope_key,
                        diary=diary,
                        agent_id=agent if diary else None,
                        project_id=effective_project if diary else None,
                        limit=limit,
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
                data = await client.memory_inspect(source_id)
            if json_output:
                print_json(data)
                return
            _print_memory_source_inspect(cast("dict[str, object]", data))
        except SibylClientError as e:
            _handle_client_error(e)

    run_memory_inspect()


@app.command("memory-promote")
def memory_promote(
    candidate_id: str = typer.Argument(..., help="Raw reflection candidate ID"),
    preview: bool = typer.Option(False, "--preview", help="Preview without promoting"),
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
    """Preview or auto-review reflection candidate promotion."""
    if preview and auto:
        error("Choose either --preview or --auto.")
        raise typer.Exit(code=1)
    if dry_run and not auto:
        error("--dry-run is only available with --auto.")
        raise typer.Exit(code=1)
    if confidence_threshold is not None and not auto:
        error("--confidence-threshold is only available with --auto.")
        raise typer.Exit(code=1)
    if not preview and not auto:
        error("memory-promote currently supports --preview or --auto.")
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
                if auto:
                    data = await client.auto_review_reflection_promotion(
                        candidate_id=candidate_id,
                        promote_to_scope=promote_to_scope,
                        promote_to_scope_key=target_scope_key,
                        domain=domain,
                        project=effective_project,
                        related_to=related_ids,
                        dry_run=dry_run,
                        confidence_threshold=confidence_threshold,
                    )
                else:
                    data = await client.preview_reflection_promotion(
                        candidate_id=candidate_id,
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
                *(
                    promoted.get("events", [])
                    if isinstance(promoted.get("events"), list)
                    else []
                ),
                *(
                    reviewed.get("events", [])
                    if isinstance(reviewed.get("events"), list)
                    else []
                ),
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
    """Preview memory sharing without enabling share writes."""
    if not preview:
        error("memory-share currently only supports --preview.")
        raise typer.Exit(code=1)
    if not target_scope:
        error("Provide --target-scope for share preview.")
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
                data = await client.preview_memory_share(
                    source_ids=parsed_source_ids,
                    target_scope=target_scope,
                    target_scope_key=resolved_target_key,
                    recipient_organization_id=recipient_organization_id,
                    project_id=project_id,
                )
            if json_output:
                print_json(data)
                return
            _print_share_preview(cast("dict[str, object]", data))
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


@app.command("remember")
def remember_memory(
    title: str = typer.Argument(..., help="Title/name of the memory"),
    content: str | None = typer.Argument(
        None,
        help="Memory body. Reads stdin if omitted.",
    ),
    content_option: str | None = typer.Option(None, "--content", help="Memory body"),
    kind: str = typer.Option(
        "episode",
        "--kind",
        "-k",
        help="Memory type: decision, plan, idea, claim, artifact, session, procedure, pattern, episode",
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
) -> None:
    """Remember a decision, plan, idea, claim, artifact, session, or learning."""

    resolved_content = content_option if content_option is not None else content
    if resolved_content is None and not sys.stdin.isatty():
        resolved_content = sys.stdin.read()

    resolved_content = (resolved_content or "").strip()
    if not resolved_content:
        error("Provide memory content as an argument or via stdin.")
        raise typer.Exit(code=1)

    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    related_ids = _parse_csv_ids(related_to)
    task_ids = _parse_csv_ids(task)
    metadata = {
        "capture_mode": "remember",
        "capture_surface": surface,
        "remember_kind": kind,
    }
    if domain:
        metadata["domain"] = domain

    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_remember() -> None:
        try:
            async with get_client() as client:
                resolved_project = (
                    await resolve_project_reference(client, effective_project)
                    if effective_project
                    else None
                )
                if resolved_project:
                    metadata["project_id"] = resolved_project
                if diary and not agent:
                    error("Provide --agent when using --diary.")
                    raise typer.Exit(code=1)
                if raw or diary:
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
                    console.print(f"  [dim]ID: {memory_id}[/dim]")
                    if policy_reason := data.get("policy_reason"):
                        console.print(f"  [dim]Policy: {policy_reason}[/dim]")
                    return

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
                raw_memory = await client.remember_raw_memory(
                    title=title,
                    raw_content=resolved_content,
                    source_id=source_id,
                    memory_scope=memory_scope,
                    scope_key=raw_scope_key,
                    diary=False,
                    agent_id=None,
                    project_id=None,
                    tags=parsed_tags,
                    metadata=metadata,
                    provenance={
                        "remember_kind": kind,
                        "related_to": resolved_links or [],
                    },
                    capture_surface=surface,
                )
                raw_memory_id = raw_memory.get("id")
                raw_source_id = raw_memory.get("source_id")
                raw_policy_reason = raw_memory.get("policy_reason")
                graph_metadata = dict(metadata)
                if raw_memory_id:
                    graph_metadata["raw_memory_id"] = raw_memory_id
                if raw_source_id:
                    graph_metadata["raw_source_id"] = raw_source_id
                if raw_policy_reason:
                    graph_metadata["raw_policy_reason"] = raw_policy_reason
                data = await client.create_entity(
                    name=title,
                    content=resolved_content,
                    entity_type=kind,
                    category=domain,
                    tags=parsed_tags,
                    related_to=resolved_links,
                    metadata=graph_metadata,
                    sync=wait_searchable,
                )

                entity_id = data.get("id", "unknown")

                if json_output:
                    data["raw_memory_id"] = raw_memory_id
                    data["raw_source_id"] = raw_source_id
                    data["raw_policy_reason"] = raw_policy_reason
                    print_json(data)
                    return

                if wait_searchable:
                    success(f"Remembered {kind}: {title}")
                else:
                    info(f"Queued {kind}: {title}")
                console.print(f"  [dim]ID: {entity_id}[/dim]")
                if raw_memory_id:
                    console.print(f"  [dim]Raw: {raw_memory_id}[/dim]")
                if raw_policy_reason:
                    console.print(f"  [dim]Policy: {raw_policy_reason}[/dim]")
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
        help="Intent: build, plan, ideate, research, debug, decide, learn, general",
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
