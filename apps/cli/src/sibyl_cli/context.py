"""Context management CLI commands.

Commands: list, show, create, use, update, delete.
Contexts bundle server URL, org, and project settings for easy switching
between environments (local, staging, prod).
"""

from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, clear_client_cache, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    run_async,
    success,
    warn,
)
from sibyl_cli.config_store import (
    Context,
    create_context,
    delete_context,
    get_active_context,
    get_active_context_name,
    get_context,
    get_current_context,
    get_effective_server_url,
    list_contexts,
    remove_path_context,
    resolve_context_from_cwd,
    resolve_effective_context,
    resolve_project_from_cwd,
    set_active_context,
    set_path_context,
    update_context,
)
from sibyl_cli.context_quick import quick_context_payload, render_quick_context
from sibyl_cli.project_refs import (
    PROJECT_RELINK_HINT,
    list_accessible_projects,
    matching_project_refs,
)

CONTEXT_PACK_PREVIEW_CHARS = 320

app = typer.Typer(
    name="context",
    help="Manage CLI contexts (server/org/project bundles)",
    invoke_without_command=True,
)


@app.callback()
def callback(
    ctx: typer.Context,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
    quick: Annotated[
        bool,
        typer.Option(
            "--quick",
            "--validate",
            help="Show local server/org/project/auth status only; use full context for project detail",
        ),
    ] = False,
) -> None:
    """Show current context when invoked without subcommand."""
    if ctx.invoked_subcommand is not None:
        return

    if quick:
        render_quick_context(quick_context_payload(), json_out=json_out)
        return

    # No subcommand - show the effective context (override > directory pin > active)
    active = resolve_effective_context()
    pinned_context = resolve_context_from_cwd()
    linked_project = resolve_project_from_cwd()

    # If no named context and no path mapping, show helpful message
    if not active and not linked_project:
        error("No active context")
        info("Link this directory: sibyl project link")
        info("Or create a context: sibyl context create local --use")
        raise typer.Exit(1)

    # If no named context but we have a path mapping, show that
    if not active and linked_project:
        _show_path_context(linked_project, json_out)
        return

    assert active is not None

    pinned_by_dir = bool(pinned_context) and pinned_context == active.name

    # Resolve effective project (linked > default)
    linked_project = resolve_project_from_cwd()
    effective_project = linked_project or active.default_project

    # Fetch project details if we have a project
    project_data: dict | None = None
    if effective_project:

        @run_async
        async def _fetch_project() -> dict | None:
            try:
                client = get_client()
                project = await client.get_entity(effective_project, related_limit=0)
                if linked_project:
                    projects = await list_accessible_projects(client)
                    if not matching_project_refs(projects, effective_project):
                        _warn_missing_linked_project(effective_project)
                        return None
                return project
            except SibylClientError as exc:
                if exc.status_code == 404:
                    _warn_missing_linked_project(effective_project)
                return None

        project_data = _fetch_project()

    if json_out:
        result = _context_to_dict(active)
        result["active"] = True
        result["pinned_by_directory"] = pinned_by_dir
        if linked_project:
            result["linked_project"] = linked_project
        if project_data:
            result["project_details"] = project_data
        print_json(result)
        return

    # Table output
    console.print()
    console.print(f"  [{ELECTRIC_PURPLE}]Context:[/{ELECTRIC_PURPLE}] [bold]{active.name}[/bold]")
    source_label = "pinned by directory" if pinned_by_dir else "active"
    console.print(f"  [{SUCCESS_GREEN}]({source_label})[/{SUCCESS_GREEN}]")
    console.print()
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}]   {active.server_url}")
    console.print(f"  [{NEON_CYAN}]Org:[/{NEON_CYAN}]      {active.org_slug or '[dim]auto[/dim]'}")
    if linked_project:
        console.print(
            f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}]  {linked_project} [dim](linked)[/dim]"
        )
    else:
        if active.default_project:
            console.print(f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}]  {active.default_project}")
        else:
            console.print(
                f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}]  [{ELECTRIC_YELLOW}]none[/{ELECTRIC_YELLOW}]"
            )
            console.print(
                f"  [{ELECTRIC_YELLOW}]⚠ Link a project:[/{ELECTRIC_YELLOW}] sibyl project list && sibyl project link <id>"
            )
    if active.insecure:
        console.print(
            f"  [{ELECTRIC_YELLOW}]Insecure:[/{ELECTRIC_YELLOW}] SSL verification disabled"
        )

    # Show project summary if we have project data
    if project_data:
        meta = project_data.get("metadata", {})
        status_counts = meta.get("status_counts", {})
        total = meta.get("total_tasks", 0)
        pct = meta.get("progress_pct", 0.0)

        console.print()
        console.print(
            f"  [{ELECTRIC_PURPLE}]───────────────────────────────────────[/{ELECTRIC_PURPLE}]"
        )
        console.print(f"  [{NEON_CYAN}]{project_data.get('name', 'Unknown')}[/{NEON_CYAN}]")
        if project_data.get("description"):
            console.print(f"  [dim]{project_data['description']}[/dim]")

        # Progress bar
        if total > 0:
            bar_filled = int(pct / 5)
            bar = f"[{SUCCESS_GREEN}]{'█' * bar_filled}[/{SUCCESS_GREEN}]{'░' * (20 - bar_filled)}"
            console.print(f"  {bar} {pct:.0f}%")

        # Brief status summary
        doing = status_counts.get("doing", 0)
        blocked = status_counts.get("blocked", 0)
        review = status_counts.get("review", 0)
        todo = status_counts.get("todo", 0)

        status_parts = []
        if doing:
            status_parts.append(f"[{SUCCESS_GREEN}]{doing} doing[/{SUCCESS_GREEN}]")
        if blocked:
            status_parts.append(f"[red]{blocked} blocked[/red]")
        if review:
            status_parts.append(f"[{ELECTRIC_YELLOW}]{review} review[/{ELECTRIC_YELLOW}]")
        if todo:
            status_parts.append(f"[dim]{todo} todo[/dim]")
        if status_parts:
            console.print(f"  {' · '.join(status_parts)}")

        # Critical tasks (high priority / CRITICAL in name)
        critical_tasks = meta.get("critical_tasks", [])
        if critical_tasks:
            console.print()
            console.print("  [red]⚠ Critical:[/red]")
            for task in critical_tasks[:2]:  # Show top 2 critical
                console.print(
                    f"    [red]●[/red] {task.get('name', '')} [{CORAL}]{task.get('id', '')}[/{CORAL}]"
                )

        # Epics with progress
        epics = meta.get("epics", [])
        if epics:
            console.print()
            console.print(f"  [{ELECTRIC_PURPLE}]Epics:[/{ELECTRIC_PURPLE}]")
            for epic in epics[:2]:  # Show top 2 epics
                epic_pct = epic.get("progress_pct", 0)
                mini_bar = "█" * int(epic_pct / 20) + "░" * (5 - int(epic_pct / 20))
                console.print(
                    f"    [{SUCCESS_GREEN}]{mini_bar}[/{SUCCESS_GREEN}] {epic_pct:.0f}% {epic.get('name', '')}"
                )

        # Actionable tasks
        actionable = _project_actionable_items(project_data)
        if actionable:
            console.print()
            for task in actionable[:3]:  # Show top 3
                status = str(task.get("relationship") or "")
                status_color = {
                    "doing": SUCCESS_GREEN,
                    "blocked": "red",
                    "review": ELECTRIC_YELLOW,
                }.get(status, CORAL)
                console.print(
                    f"  [{status_color}]●[/{status_color}] {task.get('name', '')} [{CORAL}]{task.get('id', '')}[/{CORAL}]"
                )

    console.print()


def _context_to_dict(ctx: Context) -> dict:
    """Convert Context to JSON-serializable dict."""
    return {
        "name": ctx.name,
        "server_url": ctx.server_url,
        "org_slug": ctx.org_slug,
        "default_project": ctx.default_project,
        "insecure": ctx.insecure,
    }


def _warn_missing_linked_project(project_id: str) -> None:
    warn(f"Linked project {project_id} is missing server-side.")
    console.print(f"  [{ELECTRIC_YELLOW}]{PROJECT_RELINK_HINT}[/{ELECTRIC_YELLOW}]")


def _project_actionable_items(project_data: dict) -> list[dict[str, object]]:
    related = project_data.get("related")
    if isinstance(related, list) and related:
        return [item for item in related if isinstance(item, dict)]

    metadata = project_data.get("metadata")
    if not isinstance(metadata, dict):
        return []

    actionable = metadata.get("actionable_tasks")
    if not isinstance(actionable, list):
        return []

    items: list[dict[str, object]] = []
    for task in actionable:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id")
        name = task.get("name")
        if not task_id or not name:
            continue
        items.append(
            {
                "id": task_id,
                "name": name,
                "relationship": task.get("status") or "",
            }
        )
    return items


def _show_path_context(project_id: str, json_out: bool) -> None:
    """Show context when only path mapping is available (no named context)."""
    _, matched_path = get_current_context()
    server_url = get_effective_server_url()

    # Fetch project details
    project_data: dict[str, object] | None = None

    @run_async
    async def _fetch_project() -> dict[str, object] | None:
        try:
            client = get_client()
            return dict(await client.get_entity(project_id, related_limit=0))
        except SibylClientError as exc:
            if exc.status_code == 404:
                _warn_missing_linked_project(project_id)
            return None

    project_data = _fetch_project()

    if json_out:
        result: dict[str, object] = {
            "project_id": project_id,
            "matched_path": matched_path,
            "server_url": server_url,
            "source": "path_mapping",
        }
        if project_data:
            result["project_details"] = project_data
        print_json(result)
        return

    # Display
    console.print()
    project_name = project_data.get("name", project_id) if project_data else project_id
    console.print(f"  [{ELECTRIC_PURPLE}]Project:[/{ELECTRIC_PURPLE}] [bold]{project_name}[/bold]")
    console.print(f"  [{SUCCESS_GREEN}](detected from path)[/{SUCCESS_GREEN}]")
    console.print()
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}]  {server_url}")
    if matched_path:
        console.print(f"  [{NEON_CYAN}]Path:[/{NEON_CYAN}]    {matched_path}")

    # Show project summary if we have data
    if project_data:
        meta_obj = project_data.get("metadata")
        meta: dict[str, object] = {}
        if isinstance(meta_obj, dict):
            meta = {key: value for key, value in meta_obj.items() if isinstance(key, str)}
        status_counts_obj = meta.get("status_counts", {})
        status_counts: dict[str, object] = {}
        if isinstance(status_counts_obj, dict):
            status_counts = {
                key: value for key, value in status_counts_obj.items() if isinstance(key, str)
            }
        total_obj = meta.get("total_tasks", 0)
        total = total_obj if isinstance(total_obj, int) else 0
        pct_obj = meta.get("progress_pct", 0.0)
        pct = float(pct_obj) if isinstance(pct_obj, int | float) else 0.0

        if project_data.get("description"):
            console.print(f"  [dim]{project_data['description']}[/dim]")

        # Progress bar
        if total > 0:
            console.print()
            bar_filled = int(pct / 5)
            bar = f"[{SUCCESS_GREEN}]{'█' * bar_filled}[/{SUCCESS_GREEN}]{'░' * (20 - bar_filled)}"
            console.print(f"  {bar} {pct:.0f}%")

            # Brief status summary
            doing_obj = status_counts.get("doing", 0)
            blocked_obj = status_counts.get("blocked", 0)
            review_obj = status_counts.get("review", 0)
            todo_obj = status_counts.get("todo", 0)
            doing = doing_obj if isinstance(doing_obj, int) else 0
            blocked = blocked_obj if isinstance(blocked_obj, int) else 0
            review = review_obj if isinstance(review_obj, int) else 0
            todo = todo_obj if isinstance(todo_obj, int) else 0

            status_parts = []
            if doing:
                status_parts.append(f"[{SUCCESS_GREEN}]{doing} doing[/{SUCCESS_GREEN}]")
            if blocked:
                status_parts.append(f"[red]{blocked} blocked[/red]")
            if review:
                status_parts.append(f"[{ELECTRIC_YELLOW}]{review} review[/{ELECTRIC_YELLOW}]")
            if todo:
                status_parts.append(f"[dim]{todo} todo[/dim]")
            if status_parts:
                console.print(f"  {' · '.join(status_parts)}")

    console.print()


def _format_context_pack_preview(content: str, max_chars: int = CONTEXT_PACK_PREVIEW_CHARS) -> str:
    preview = " ".join(content.strip().split())
    if len(preview) <= max_chars:
        return preview

    cutoff = preview.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return preview[:cutoff].rstrip() + "…"


@app.command("pack")
def pack_cmd(
    goal: Annotated[str, typer.Argument(help="Agent goal or user task")],
    intent: Annotated[
        str,
        typer.Option(
            "--intent",
            "-i",
            help="Agent intent: build, plan, ideate, research, debug, decide, learn, general",
        ),
    ] = "build",
    layer: Annotated[
        str,
        typer.Option(
            "--layer",
            help="Context depth: wake, recall, deep_search",
        ),
    ] = "recall",
    domain: Annotated[
        str | None,
        typer.Option("--domain", "-d", help="Domain/category to bias retrieval"),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project ID to scope context"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent diary identity to include"),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all", "-a", help="Use all accessible projects"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", min=1, max=50, help="Maximum total context items"),
    ] = 24,
    related: Annotated[
        bool,
        typer.Option("--related/--no-related", help="Include one-hop related graph context"),
    ] = True,
    related_limit: Annotated[
        int,
        typer.Option("--related-limit", min=0, max=5, help="Related items per context item"),
    ] = 3,
    markdown: Annotated[
        bool,
        typer.Option("--markdown", "-m", help="Output compact Markdown for agent injection"),
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
    audit: Annotated[
        bool,
        typer.Option(
            "--audit",
            help="Include full retrieval metadata per item (for auditing noisy packs)",
        ),
    ] = False,
    budget: Annotated[
        int | None,
        typer.Option(
            "--budget",
            min=100,
            max=8000,
            help="Cap rendered markdown at roughly this many tokens",
        ),
    ] = None,
) -> None:
    """Compile a precise context pack for an agent."""
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                pack = await client.context_pack(
                    goal=goal,
                    intent=intent,
                    layer=layer,
                    domain=domain,
                    project=effective_project,
                    agent_id=agent,
                    limit=limit,
                    include_related=related,
                    related_limit=related_limit,
                    audit=audit,
                    markdown_token_budget=budget,
                )
        except SibylClientError as e:
            handle_client_error(e)
            raise typer.Exit(1) from e

        if json_out:
            print_json(pack)
            return
        if markdown:
            console.print(pack.get("markdown") or "")
            return

        console.print()
        console.print(f"  [{ELECTRIC_PURPLE}]Context Pack[/{ELECTRIC_PURPLE}]")
        console.print(f"  [{NEON_CYAN}]Goal:[/{NEON_CYAN}] {pack.get('goal', goal)}")
        console.print(f"  [{NEON_CYAN}]Intent:[/{NEON_CYAN}] {pack.get('intent', intent)}")
        console.print(f"  [{NEON_CYAN}]Layer:[/{NEON_CYAN}] {pack.get('layer', layer)}")
        if pack.get("domain"):
            console.print(f"  [{NEON_CYAN}]Domain:[/{NEON_CYAN}] {pack['domain']}")
        if pack.get("project"):
            console.print(f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}] {pack['project']}")
        console.print(f"  [dim]{pack.get('total_items', 0)} item(s)[/dim]")
        console.print()

        sections = pack.get("sections", [])
        if not sections:
            info("No context found")
            return

        for section in sections:
            console.print(
                f"  [{ELECTRIC_PURPLE}]{section.get('title', 'Context')}[/{ELECTRIC_PURPLE}]"
            )
            for item in section.get("items", []):
                name = item.get("name", "Untitled")
                item_type = item.get("type", "memory")
                item_id = item.get("id", "")
                console.print(
                    f"    [{NEON_CYAN}]{name}[/{NEON_CYAN}] "
                    f"[dim]({item_type})[/dim] [{CORAL}]{item_id}[/{CORAL}]"
                )
                reason = item.get("reason")
                if reason:
                    console.print(f"      [dim]{reason}[/dim]")
                content = item.get("content")
                if content:
                    console.print(f"      {_format_context_pack_preview(content)}", soft_wrap=True)
                related_items = item.get("related") or []
                if related_items:
                    labels = [
                        f"{related.get('relationship', 'RELATED_TO')} {related.get('name', related.get('id', ''))}"
                        for related in related_items[:related_limit]
                    ]
                    console.print(f"      [dim]Related: {'; '.join(labels)}[/dim]")
            console.print()

        if hint := pack.get("usage_hint"):
            console.print(f"  [dim]{hint}[/dim]")

    _run()


@app.command("list")
def list_cmd(
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """List all configured contexts. Default: table output."""
    contexts = list_contexts()
    active_name = get_active_context_name()

    if json_out:
        result = [_context_to_dict(ctx) for ctx in contexts]
        for item in result:
            item["active"] = item["name"] == active_name
        print_json(result)
        return

    if not contexts:
        info("No contexts configured")
        console.print()
        console.print(f"  [{NEON_CYAN}]Create one:[/{NEON_CYAN}]")
        console.print("    sibyl context create local --server http://localhost:3334")
        return

    table = create_table("Contexts", "", "Name", "Server", "Org", "Project")
    for ctx in contexts:
        is_active = ctx.name == active_name
        marker = f"[{ELECTRIC_PURPLE}]*[/{ELECTRIC_PURPLE}]" if is_active else " "
        name_style = f"bold {NEON_CYAN}" if is_active else ""

        table.add_row(
            marker,
            f"[{name_style}]{ctx.name}[/{name_style}]" if name_style else ctx.name,
            ctx.server_url,
            ctx.org_slug or "[dim]auto[/dim]",
            ctx.default_project or "[dim]none[/dim]",
        )

    console.print(table)
    if active_name:
        console.print("\n[dim]* = active context[/dim]")


@app.command("show")
def show_cmd(
    name: Annotated[str, typer.Argument(help="Context name (omit for active context)")] = "",
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show context details. Default: table output."""
    if name:
        ctx = get_context(name)
        if not ctx:
            error(f"Context '{name}' not found")
            raise typer.Exit(1)
    else:
        ctx = get_active_context()
        if not ctx:
            error("No active context")
            info("Set one with: sibyl context use <name>")
            raise typer.Exit(1)

    active_name = get_active_context_name()
    is_active = ctx.name == active_name

    if json_out:
        result = _context_to_dict(ctx)
        result["active"] = is_active
        print_json(result)
        return

    # Table output
    console.print()
    console.print(f"  [{ELECTRIC_PURPLE}]Context:[/{ELECTRIC_PURPLE}] [bold]{ctx.name}[/bold]")
    if is_active:
        console.print(f"  [{SUCCESS_GREEN}](active)[/{SUCCESS_GREEN}]")
    console.print()
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}]   {ctx.server_url}")
    console.print(f"  [{NEON_CYAN}]Org:[/{NEON_CYAN}]      {ctx.org_slug or '[dim]auto[/dim]'}")
    console.print(
        f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}]  {ctx.default_project or '[dim]none[/dim]'}"
    )
    if ctx.insecure:
        console.print(
            f"  [{ELECTRIC_YELLOW}]Insecure:[/{ELECTRIC_YELLOW}] SSL verification disabled"
        )
    console.print()


@app.command("create")
def create_cmd(
    name: Annotated[str, typer.Argument(help="Context name (e.g., 'prod', 'local')")],
    server: Annotated[
        str, typer.Option("--server", "-s", help="Server URL")
    ] = "http://localhost:3334",
    org: Annotated[str, typer.Option("--org", "-o", help="Organization slug (optional)")] = "",
    project: Annotated[
        str, typer.Option("--project", "-p", help="Default project ID (optional)")
    ] = "",
    use: Annotated[bool, typer.Option("--use", "-u", help="Set as active context")] = False,
    insecure: Annotated[
        bool, typer.Option("--insecure", "-k", help="Skip SSL verification (self-signed certs)")
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Create a new context."""
    try:
        ctx = create_context(
            name=name,
            server_url=server,
            org_slug=org or None,
            default_project=project or None,
            set_active=use,
            insecure=insecure,
        )
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    if json_out:
        result = _context_to_dict(ctx)
        result["active"] = use
        print_json(result)
        return

    success(f"Created context '{name}'")
    if use:
        info("Set as active context")
    if insecure:
        info("SSL verification disabled")
    console.print()
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}]  {ctx.server_url}")
    console.print(f"  [{NEON_CYAN}]Org:[/{NEON_CYAN}]     {ctx.org_slug or '[dim]auto[/dim]'}")
    console.print(
        f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}] {ctx.default_project or '[dim]none[/dim]'}"
    )


@app.command("use")
def use_cmd(
    name: Annotated[str, typer.Argument(help="Context name to activate")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Set the active context."""
    ctx = get_context(name)
    if not ctx:
        error(f"Context '{name}' not found")
        contexts = list_contexts()
        if contexts:
            info(f"Available: {', '.join(c.name for c in contexts)}")
        raise typer.Exit(1)

    set_active_context(name)
    clear_client_cache()  # Ensure new connections use the new context

    if json_out:
        result = _context_to_dict(ctx)
        result["active"] = True
        print_json(result)
        return

    success(f"Switched to context '{name}'")
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}] {ctx.server_url}")


@app.command("update")
def update_cmd(
    name: Annotated[str, typer.Argument(help="Context name to update")],
    server: Annotated[str, typer.Option("--server", "-s", help="New server URL")] = "",
    org: Annotated[
        str, typer.Option("--org", "-o", help="New org slug (use 'auto' to clear)")
    ] = "",
    project: Annotated[
        str, typer.Option("--project", "-p", help="New default project (use 'none' to clear)")
    ] = "",
    insecure: Annotated[
        bool, typer.Option("--insecure", "-k", help="Skip SSL verification (self-signed certs)")
    ] = False,
    secure: Annotated[bool, typer.Option("--secure", help="Re-enable SSL verification")] = False,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Update an existing context."""
    # Determine what to update
    kwargs: dict = {}
    if server:
        kwargs["server_url"] = server
    if org:
        kwargs["org_slug"] = None if org.lower() == "auto" else org
    if project:
        kwargs["default_project"] = None if project.lower() == "none" else project
    if insecure:
        kwargs["insecure"] = True
    elif secure:
        kwargs["insecure"] = False

    if not kwargs:
        error("Nothing to update. Provide --server, --org, --project, --insecure, or --secure")
        raise typer.Exit(1)

    try:
        ctx = update_context(name, **kwargs)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    if json_out:
        result = _context_to_dict(ctx)
        result["active"] = get_active_context_name() == name
        print_json(result)
        return

    success(f"Updated context '{name}'")
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}]  {ctx.server_url}")
    console.print(f"  [{NEON_CYAN}]Org:[/{NEON_CYAN}]     {ctx.org_slug or '[dim]auto[/dim]'}")
    console.print(
        f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}] {ctx.default_project or '[dim]none[/dim]'}"
    )


@app.command("delete")
def delete_cmd(
    name: Annotated[str, typer.Argument(help="Context name to delete")],
) -> None:
    """Delete a context."""
    ctx = get_context(name)
    if not ctx:
        error(f"Context '{name}' not found")
        raise typer.Exit(1)

    active_name = get_active_context_name()
    is_active = name == active_name

    deleted = delete_context(name)
    if deleted:
        success(f"Deleted context '{name}'")
        if is_active:
            info("No active context. Use 'sibyl context use <name>' to set one.")
    else:
        error(f"Failed to delete context '{name}'")
        raise typer.Exit(1)


@app.command("link")
def link_cmd(
    name: Annotated[str, typer.Argument(help="Context name to pin to this directory")],
    path: Annotated[
        str | None, typer.Option("--path", "-p", help="Directory path (defaults to cwd)")
    ] = None,
) -> None:
    """Pin a directory tree to a context so commands here route to its server.

    Unlike `project link`, this binds only the context (server/org), not a project,
    so new repositories under this tree route to the right server before they are
    linked to a specific project.
    """
    import os

    ctx = get_context(name)
    if not ctx:
        error(f"Context '{name}' not found")
        contexts = list_contexts()
        if contexts:
            info(f"Available: {', '.join(c.name for c in contexts)}")
        raise typer.Exit(1)

    target_path = path or os.getcwd()
    set_path_context(target_path, name)
    clear_client_cache()  # New connections under this path use the pinned context

    success(f"Pinned [{NEON_CYAN}]{target_path}[/{NEON_CYAN}] to context '{name}'")
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}] {ctx.server_url}")
    info("Commands under this directory now route to this server")


@app.command("unlink")
def unlink_cmd(
    path: Annotated[
        str | None, typer.Option("--path", "-p", help="Directory path (defaults to cwd)")
    ] = None,
) -> None:
    """Remove the context pin from a directory (keeps any project link)."""
    import os

    target_path = path or os.getcwd()
    if remove_path_context(target_path):
        clear_client_cache()
        success(f"Unpinned context from [{NEON_CYAN}]{target_path}[/{NEON_CYAN}]")
    else:
        info(f"No context pin found for {target_path}")


@app.command("clear")
def clear_cmd() -> None:
    """Clear the active context (use legacy mode)."""
    set_active_context(None)
    clear_client_cache()  # Ensure new connections use legacy config
    success("Cleared active context")
    info("Using legacy server.url from config")
