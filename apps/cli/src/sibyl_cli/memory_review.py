"""Memory review queue automation CLI commands."""

from __future__ import annotations

import asyncio
from typing import cast

import typer

from sibyl_cli import command_support
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import print_json, run_async
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.memory_views import (
    print_memory_review_drain,
    print_reflection_dream_enqueue,
    print_reflection_dream_status,
)

app = typer.Typer(help="Memory review queue automation commands")


@app.command("drain")
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
    related_ids = command_support.append_unique_ids(
        command_support.parse_csv_ids(related_to), command_support.parse_csv_ids(task)
    )
    archive_reason_ids = command_support.parse_csv_ids(archive_reasons)

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
            print_memory_review_drain(cast("dict[str, object]", data))
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_memory_review_drain()


@app.command("dream")
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
            print_reflection_dream_enqueue(cast("dict[str, object]", data), dry_run=dry_run)
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_memory_review_dream()


@app.command("status")
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
            print_reflection_dream_status(payload)
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_memory_review_status()
