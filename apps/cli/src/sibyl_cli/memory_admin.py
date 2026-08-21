"""Memory administration, review, and access-preview CLI commands."""

from __future__ import annotations

from typing import Annotated, Any, cast

import typer

from sibyl_cli import command_support, memory_review, memory_space
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    error,
    info,
    print_json,
    print_mutation_receipt,
    resolve_content_input,
    run_async,
    success,
)
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.id_resolution import resolve_raw_memory_id_prefix
from sibyl_cli.memory_display import (
    inspect_raw_memory_source,
    print_memory_source_blame,
    print_memory_source_inspect,
    raw_memory_lookup_value,
)
from sibyl_cli.memory_views import (
    print_memory_audit_events,
    print_promotion_autonomy,
    print_promotion_preview,
    print_promotion_result,
    print_share_preview,
    print_share_result,
    print_source_import_status,
)

app = typer.Typer(help="Memory administration commands")


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


@app.command("audit")
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
            print_memory_audit_events(events if isinstance(events, list) else [])
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_memory_audit()


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
    parsed_ids = command_support.parse_id_args(cited_ids)
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
            command_support.handle_client_error(e)

    run_cite_memories()


@app.command("inspect")
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
            command_support.handle_client_error(e)

    run_memory_inspect()


def blame_memory(
    source_id: str = typer.Argument(..., help="Raw memory source ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Show a memory's revisions, corrections, audits, and lineage."""

    @run_async
    async def run_blame_memory() -> None:
        try:
            async with get_client() as client:
                data = await _load_memory_blame(client, source_id)
            if json_output:
                print_json(data)
                return
            print_memory_source_blame(cast("dict[str, object]", data))
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_blame_memory()


async def _load_memory_blame(client: Any, source_id: str) -> dict[str, Any]:
    resolved_source_id = await resolve_raw_memory_id_prefix(
        client,
        raw_memory_lookup_value(source_id),
    )
    return await client.memory_blame(resolved_source_id)


def correct_memory(
    source_id: str = typer.Argument(..., help="Raw memory source ID"),
    action: str | None = typer.Option(
        None,
        "--action",
        help="Correction: wrong, stale, duplicate, superseded, or revise",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Why this correction is needed"),
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
    """Inspect, correct, or revise a raw memory with a durable receipt."""
    if action is None:
        mutation_inputs = (
            reason,
            replacement,
            duplicate_of,
            content,
            content_file,
            expected_revision,
        )
        if any(value is not None for value in mutation_inputs) or preview:
            error("Correction options require --action")
            raise typer.Exit(code=1)

        @run_async
        async def run_inspect_memory() -> None:
            try:
                async with get_client() as client:
                    data = await _load_memory_blame(client, source_id)
                if json_output:
                    print_json(data)
                    return
                print_memory_source_blame(cast("dict[str, object]", data))
            except SibylClientError as e:
                command_support.handle_client_error(e)

        run_inspect_memory()
        return

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
    reason_text = (reason or "").strip()
    if not reason_text:
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
                    reason=reason_text,
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
            command_support.handle_client_error(e)

    run_correct_memory()


@app.command("import-status")
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
            print_source_import_status(cast("dict[str, object]", data))
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_memory_import_status()


@app.command("promote")
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
    related_ids = command_support.append_unique_ids(
        command_support.parse_csv_ids(related_to), command_support.parse_csv_ids(task)
    )

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
                print_promotion_autonomy(payload)
            elif apply_changes:
                print_promotion_result(payload)
            else:
                print_promotion_preview(payload)
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_memory_promote()


@app.command("share")
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

    parsed_source_ids = command_support.parse_id_args(source_ids)
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
                print_share_preview(cast("dict[str, object]", data))
            else:
                print_share_result(cast("dict[str, object]", data))
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_memory_share()


app.add_typer(memory_space.app, name="space")
app.add_typer(memory_review.app, name="review")


def register_root_commands(root: typer.Typer) -> None:
    """Register legacy top-level memory spellings and hidden group aliases."""
    root.add_typer(memory_space.app, name="memory-space", hidden=True)
    root.add_typer(memory_review.app, name="memory-review", hidden=True)
    root.command("memory-audit", hidden=True)(memory_audit)
    root.command("cite")(cite_memories)
    root.command("memory-inspect", hidden=True)(memory_inspect)
    root.command("blame", hidden=True)(blame_memory)
    root.command("correct")(correct_memory)
    root.command("memory-import-status", hidden=True)(memory_import_status)
    root.command("memory-promote", hidden=True)(memory_promote)
    root.command("memory-share", hidden=True)(memory_share)
