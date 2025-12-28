"""Context management CLI commands.

Commands: list, show, create, use, update, delete.
Contexts bundle server URL, org, and project settings for easy switching
between environments (local, staging, prod).
"""

from typing import Annotated

import typer

from sibyl.cli.client import clear_client_cache
from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_table,
    error,
    info,
    print_json,
    success,
)
from sibyl.cli.config_store import (
    Context,
    create_context,
    delete_context,
    get_active_context,
    get_active_context_name,
    get_context,
    list_contexts,
    set_active_context,
    update_context,
)

app = typer.Typer(
    name="context",
    help="Manage CLI contexts (server/org/project bundles)",
    invoke_without_command=True,
)


@app.callback()
def callback(
    ctx: typer.Context,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show current context when invoked without subcommand."""
    if ctx.invoked_subcommand is not None:
        return

    # No subcommand - show active context
    active = get_active_context()
    if not active:
        error("No active context")
        info("Set one with: sibyl context use <name>")
        raise typer.Exit(1)

    if not table_out:
        result = _context_to_dict(active)
        result["active"] = True
        print_json(result)
        return

    # Table output
    console.print()
    console.print(
        f"  [{ELECTRIC_PURPLE}]Context:[/{ELECTRIC_PURPLE}] [bold]{active.name}[/bold]"
    )
    console.print(f"  [{SUCCESS_GREEN}](active)[/{SUCCESS_GREEN}]")
    console.print()
    console.print(f"  [{NEON_CYAN}]Server:[/{NEON_CYAN}]   {active.server_url}")
    console.print(f"  [{NEON_CYAN}]Org:[/{NEON_CYAN}]      {active.org_slug or '[dim]auto[/dim]'}")
    console.print(
        f"  [{NEON_CYAN}]Project:[/{NEON_CYAN}]  {active.default_project or '[dim]none[/dim]'}"
    )
    if active.insecure:
        console.print(
            f"  [{ELECTRIC_YELLOW}]Insecure:[/{ELECTRIC_YELLOW}] SSL verification disabled"
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


@app.command("list")
def list_cmd(
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """List all configured contexts. Default: JSON output."""
    contexts = list_contexts()
    active_name = get_active_context_name()

    if not table_out:
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
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show context details. Default: JSON output."""
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

    if not table_out:
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
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
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

    if not table_out:
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
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
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

    if not table_out:
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
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
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

    if not table_out:
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
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a context."""
    ctx = get_context(name)
    if not ctx:
        error(f"Context '{name}' not found")
        raise typer.Exit(1)

    active_name = get_active_context_name()
    is_active = name == active_name

    if not yes:
        msg = f"Delete context '{name}'"
        if is_active:
            msg += " (currently active)"
        msg += "?"
        if not typer.confirm(msg):
            info("Cancelled")
            raise typer.Exit(0)

    deleted = delete_context(name)
    if deleted:
        success(f"Deleted context '{name}'")
        if is_active:
            info("No active context. Use 'sibyl context use <name>' to set one.")
    else:
        error(f"Failed to delete context '{name}'")
        raise typer.Exit(1)


@app.command("clear")
def clear_cmd() -> None:
    """Clear the active context (use legacy mode)."""
    set_active_context(None)
    clear_client_cache()  # Ensure new connections use legacy config
    success("Cleared active context")
    info("Using legacy server.url from config")
