"""Main CLI application - client-side commands for Sibyl.

This is the entry point for the sibyl-dev package.
All commands communicate with the REST API.

Server commands (serve, dev, db, generate, etc.) are in sibyl-server.
"""

import sys
from importlib.metadata import version as pkg_version
from os import environ
from typing import Annotated, Any, cast

import typer
from typer.core import TyperGroup

from sibyl_cli import command_support, config_store, memory_admin, root_commands, state
from sibyl_cli.archive import app as archive_app
from sibyl_cli.auth import app as auth_app
from sibyl_cli.auth import clear_token_cmd as logout_cmd
from sibyl_cli.auth import login_cmd
from sibyl_cli.auth import status_cmd as whoami_cmd
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    NEON_CYAN,
    console,
    create_table,
    error,
    info,
    mark_pending_writes_reported,
    notify_pending_writes,
    pending_writes_summary,
    print_json,
    print_json_result,
    run_async,
    success,
    warn,
)
from sibyl_cli.config_cmd import app as config_app
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
from sibyl_cli.export import app as export_app
from sibyl_cli.host import serve as serve_cmd
from sibyl_cli.host import service_app
from sibyl_cli.host import start as start_cmd
from sibyl_cli.host import stop as stop_cmd
from sibyl_cli.id_resolution import resolve_id_prefix
from sibyl_cli.ingest import app as ingest_app
from sibyl_cli.local import app as local_app
from sibyl_cli.local import start as up_cmd
from sibyl_cli.local import stop as down_cmd
from sibyl_cli.logs import app as logs_app
from sibyl_cli.memory_display import (
    inspect_raw_memory_source,
    is_raw_memory_reference,
    print_memory_source_inspect,
)
from sibyl_cli.migrate import app as migrate_app
from sibyl_cli.org import app as org_app
from sibyl_cli.pending import app as pending_writes_app
from sibyl_cli.pending_writes import pending_write_count, pending_write_status
from sibyl_cli.project import app as project_app
from sibyl_cli.session import app as session_app
from sibyl_cli.skill import app as skill_app
from sibyl_cli.state import set_context_override
from sibyl_cli.synthesis import app as synthesis_app
from sibyl_cli.task import app as task_app
from sibyl_cli.task import list_tasks
from sibyl_cli.team import app as team_app
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


def _resolve_command_path(group: Any, ctx: Any, args: list[str]) -> tuple[str, ...]:
    """Walk the command tree as far as the arguments unambiguously reach.

    Resolution stops at the first token that is not a subcommand of the group
    reached so far, which leaves an option-interleaved invocation with a short
    path. Callers treat a short path as unrecognized, so the ambiguity fails
    closed rather than granting a leaf's privileges to its group.
    """
    path: list[str] = []
    command = group
    for arg in args:
        lookup = getattr(command, "get_command", None)
        if lookup is None:
            break
        sub = lookup(ctx, arg)
        if sub is None:
            break
        path.append(getattr(sub, "name", None) or arg)
        command = sub
    return tuple(path)


class SibylRootGroup(TyperGroup):
    """Records the resolved subcommand path before any callback runs."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        rest = super().parse_args(ctx, args)
        protected = getattr(ctx, "_protected_args", None)
        if protected is None:
            protected = getattr(ctx, "protected_args", [])
        state.set_command_path(_resolve_command_path(self, ctx, [*protected, *ctx.args]))
        return rest


# Main app
app = typer.Typer(
    name="sibyl",
    cls=SibylRootGroup,
    help="Sibyl - Oracle of Development Wisdom (CLI Client)",
    add_completion=False,
    no_args_is_help=False,
)
admin_app = typer.Typer(help="Administrative commands")
QUIET_ENV_VALUES = {"1", "true", "yes", "on"}


# Register subcommand groups
app.add_typer(task_app, name="task")
app.add_typer(epic_app, name="epic")
app.add_typer(project_app, name="project")
app.add_typer(archive_app, name="archive")
app.add_typer(session_app, name="session")
app.add_typer(entity_app, name="entity")
app.add_typer(explore_app, name="explore")
app.add_typer(migrate_app, name="migrate")
app.add_typer(export_app, name="export")
app.add_typer(crawl_app, name="crawl")
app.add_typer(docs_app, name="docs")
app.add_typer(debug_app, name="debug")
app.add_typer(dev_app, name="dev")
app.add_typer(auth_app, name="auth")
app.add_typer(org_app, name="org")
config_app.add_typer(context_app, name="context")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="contexts", hidden=True)
app.add_typer(docker_app, name="docker")
app.add_typer(service_app, name="service")
app.add_typer(local_app, name="local")
app.add_typer(logs_app, name="logs")
app.add_typer(update_app, name="update")
app.add_typer(skill_app, name="skill")
app.add_typer(ingest_app, name="ingest")
app.add_typer(pending_writes_app, name="pending-writes")
admin_app.add_typer(memory_admin.app, name="memory")
app.add_typer(admin_app, name="admin")
memory_admin.register_root_commands(app)
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


# `sibyl config context pack` fetches a pack, while `sibyl config context list`
# reads a TOML file, so the privilege belongs to the leaf and not to the name
# it shares with a group.
_LOCAL_CONTEXT_LEAVES = (
    "list",
    "show",
    "create",
    "use",
    "update",
    "delete",
    "link",
    "unlink",
    "clear",
)
_LOCAL_CONFIG_LEAVES = ("init", "show", "get", "set", "path", "reset", "edit")
_CONTEXT_REPAIR_COMMANDS = frozenset(
    [
        ("init",),
        # doctor stays reachable because it is what you run when the selection
        # is broken, and it drops to filesystem checks once that happens.
        ("doctor",),
        *[("config", leaf) for leaf in _LOCAL_CONFIG_LEAVES],
        *[("config", "context", leaf) for leaf in _LOCAL_CONTEXT_LEAVES],
        *[("contexts", leaf) for leaf in _LOCAL_CONTEXT_LEAVES],
    ]
)


def _reject_unknown_context() -> None:
    """Stop before any command logic when the selected context does not exist."""
    try:
        selection = config_store.explicit_context_selection()
        if selection is None:
            return
        name, source = selection
        if config_store.get_context(name) is not None:
            return
    except (OSError, RuntimeError):
        # Config is unreadable, so there is nothing to validate against. The
        # resolver still refuses to fall through if a name is used later.
        return
    if state.command_path() in _CONTEXT_REPAIR_COMMANDS:
        warn(f"Selected context '{name}' ({source}) does not exist; ignoring it.")
        state.ignore_context_selection()
        return

    known = [c.name for c in config_store.list_contexts()]
    error(f"Unknown context '{name}' selected by {source}.")
    if known:
        info(f"Known contexts: {', '.join(known)}")
        info("Switch with: sibyl context use <name>")
    else:
        info("No contexts are configured. Create one with: sibyl init")
    raise typer.Exit(1)


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

    _reject_unknown_context()
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
            command_support.handle_client_error(e)

    _show()


def _print_version_lines(data: dict[str, object]) -> None:
    """Show both versions, since `health` is where drift gets diagnosed."""
    from sibyl_cli.version_drift import client_version
    from sibyl_core.version_contract import server_is_ahead

    server_version = data.get("version")
    server_text = str(server_version) if server_version else "unknown"
    current = client_version()

    console.print(f"  [dim]Server:  {server_text}[/dim]")
    console.print(f"  [dim]Client:  {current}[/dim]")

    runtime = data.get("runtime")
    if isinstance(runtime, dict):
        commit = str(runtime.get("commit") or "unknown")
        if commit != "unknown":
            console.print(f"  [dim]Commit:  {commit[:12]}[/dim]")

    if server_text == "0.0.0":
        warn("Server reports no version — its image was built without provenance.")
    elif server_is_ahead(client=current, server=server_text):
        warn(f"Server is newer than this CLI ({server_text} > {current}) — run `sibyl update`.")


def _print_pending_write_health() -> None:
    """Report the local write buffer, the queue a failed write lands in."""
    mark_pending_writes_reported()
    count = pending_write_count()
    if count:
        warn(pending_writes_summary(count))
    else:
        console.print("  [dim]Pending writes: 0[/dim]")


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

                status = data.get("status", "unknown")
                server = data.get("server_name", "sibyl")
                healthy = status == "healthy"

                if json_output:
                    mark_pending_writes_reported()
                    print_json_result(
                        {**data, "pending_writes": pending_write_status()},
                        succeeded=healthy,
                    )
                    return

                if healthy:
                    success(f"{server} is healthy")
                    _print_version_lines(data)
                    if counts := data.get("counts"):
                        console.print(f"  [dim]Entities: {counts.get('entities', 0)}[/dim]")
                        console.print(
                            f"  [dim]Relationships: {counts.get('relationships', 0)}[/dim]"
                        )
                else:
                    error(f"{server} is unhealthy: {status}")

                _print_pending_write_health()

                if not healthy:
                    raise typer.Exit(1)
        except SibylClientError as e:
            command_support.handle_client_error(e)

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


root_commands.register_commands(app)


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
            command_support.handle_client_error(e)

    get_stats()


@app.command()
def version() -> None:
    """Show version information."""
    console.print(f"sibyl {get_version()}")


def main() -> None:
    """CLI entry point."""
    try:
        app()
    except config_store.ConfigStoreError as exc:
        error(str(exc))
        raise SystemExit(1) from None
    except config_store.UnknownContextError as exc:
        # Backstop for any resolver reached without passing the root callback.
        error(str(exc))
        raise typer.Exit(1) from exc
    finally:
        notify_pending_writes()


if __name__ == "__main__":
    main()
