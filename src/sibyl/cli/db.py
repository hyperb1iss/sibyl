"""Database operations CLI commands.

Commands for backup, restore, and database management.
"""

import json
from pathlib import Path
from typing import Annotated

import typer

from sibyl.cli.common import (
    ERROR_RED,
    NEON_CYAN,
    console,
    error,
    info,
    print_db_hint,
    run_async,
    spinner,
    success,
    warn,
)

app = typer.Typer(
    name="db",
    help="Database operations",
    no_args_is_help=True,
)


@app.command("backup")
def backup_db(
    output: Annotated[Path, typer.Option("--output", "-o", help="Backup file path")] = Path(
        "sibyl_backup.json"
    ),
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for multi-tenant graph)"),
    ] = "",
) -> None:
    """Backup the graph database to a JSON file."""
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

    @run_async
    async def _backup() -> None:
        from dataclasses import asdict

        from sibyl.tools.admin import create_backup

        try:
            with spinner("Creating backup...") as progress:
                progress.add_task("Creating backup...", total=None)
                result = await create_backup(organization_id=org_id)

            if not result.success or result.backup_data is None:
                error(f"Backup failed: {result.message}")
                return

            # Write backup to file (sync I/O after async work is done)
            backup_dict = asdict(result.backup_data)
            with open(output, "w") as f:  # noqa: ASYNC230
                json.dump(backup_dict, f, indent=2, default=str)

            success(f"Backup created: {output}")
            info(f"Entities: {result.entity_count}, Relationships: {result.relationship_count}")
            info(f"Duration: {result.duration_seconds:.2f}s")

        except Exception as e:
            error(f"Backup failed: {e}")
            print_db_hint()

    _backup()


@app.command("restore")
def restore_db(
    backup_file: Annotated[Path, typer.Argument(help="Backup file to restore")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for multi-tenant graph)"),
    ] = "",
    skip_existing: Annotated[
        bool,
        typer.Option("--skip-existing/--overwrite", help="Skip entities that already exist"),
    ] = True,
) -> None:
    """Restore the database from a backup file."""
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

    if not backup_file.exists():
        error(f"Backup file not found: {backup_file}")
        raise typer.Exit(code=1)

    if not yes:
        warn("This will add entities from the backup to the database.")
        confirm = typer.confirm("Continue?")
        if not confirm:
            info("Cancelled")
            return

    @run_async
    async def _restore() -> None:
        from sibyl.tools.admin import BackupData, restore_backup

        try:
            # Load backup file (sync I/O before async work)
            with open(backup_file) as f:  # noqa: ASYNC230
                backup_dict = json.load(f)

            # Convert dict to BackupData
            backup_data = BackupData(
                version=backup_dict.get("version", "1.0"),
                created_at=backup_dict.get("created_at", ""),
                organization_id=backup_dict.get("organization_id", org_id),
                entity_count=backup_dict.get("entity_count", 0),
                relationship_count=backup_dict.get("relationship_count", 0),
                entities=backup_dict.get("entities", []),
                relationships=backup_dict.get("relationships", []),
            )

            info(
                f"Restoring {backup_data.entity_count} entities and {backup_data.relationship_count} relationships..."
            )

            with spinner("Restoring...") as progress:
                progress.add_task("Restoring...", total=None)
                result = await restore_backup(
                    backup_data,
                    organization_id=org_id,
                    skip_existing=skip_existing,
                )

            if result.success:
                success("Restore complete!")
            else:
                warn("Restore completed with errors")

            info(
                f"Restored: {result.entities_restored} entities, {result.relationships_restored} relationships"
            )
            if result.entities_skipped or result.relationships_skipped:
                info(
                    f"Skipped: {result.entities_skipped} entities, {result.relationships_skipped} relationships"
                )
            info(f"Duration: {result.duration_seconds:.2f}s")

            if result.errors:
                warn(f"Errors: {len(result.errors)}")
                for err in result.errors[:5]:
                    console.print(f"  [dim]{err}[/dim]")
                if len(result.errors) > 5:
                    console.print(f"  [dim]...and {len(result.errors) - 5} more[/dim]")

        except Exception as e:
            error(f"Restore failed: {e}")
            print_db_hint()

    _restore()


@app.command("clear")
def clear_db(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Clear all data from the database. USE WITH CAUTION!"""
    if not yes:
        console.print(
            f"\n[{ERROR_RED}]WARNING: This will DELETE ALL DATA from the graph![/{ERROR_RED}]\n"
        )
        confirm = typer.confirm("Are you absolutely sure?")
        if not confirm:
            info("Cancelled")
            return

        double_confirm = typer.confirm("Type 'yes' again to confirm")
        if not double_confirm:
            info("Cancelled")
            return

    @run_async
    async def _clear() -> None:
        from sibyl.graph.client import get_graph_client

        try:
            with spinner("Clearing database...") as progress:
                progress.add_task("Clearing database...", total=None)

                client = await get_graph_client()
                # Delete all nodes and relationships
                await client.execute_write("MATCH (n) DETACH DELETE n")

            success("Database cleared")
            warn("All data has been deleted")

        except Exception as e:
            error(f"Clear failed: {e}")
            print_db_hint()

    _clear()


@app.command("stats")
def db_stats() -> None:
    """Show detailed database statistics."""

    @run_async
    async def _stats() -> None:
        from sibyl.graph.client import get_graph_client

        try:
            with spinner("Loading stats...") as progress:
                progress.add_task("Loading stats...", total=None)

                client = await get_graph_client()

                # Get node count
                node_rows = await client.execute_read("MATCH (n) RETURN count(n) as count")
                node_count = node_rows[0][0] if node_rows else 0

                # Get relationship count
                rel_rows = await client.execute_read("MATCH ()-[r]->() RETURN count(r) as count")
                rel_count = rel_rows[0][0] if rel_rows else 0

                # Get node types
                type_rows = await client.execute_read(
                    "MATCH (n) RETURN n.entity_type as type, count(*) as count ORDER BY count DESC"
                )

            console.print(f"\n[{NEON_CYAN}]Database Statistics[/{NEON_CYAN}]\n")
            console.print(f"  Total Nodes: {node_count}")
            console.print(f"  Total Relationships: {rel_count}")

            if type_rows:
                console.print("\n  [dim]By Entity Type:[/dim]")
                for row in type_rows:
                    if row[0]:
                        console.print(f"    {row[0]}: {row[1]}")

        except Exception as e:
            error(f"Failed to get stats: {e}")
            print_db_hint()

    _stats()


@app.command("fix-embeddings")
def db_fix_embeddings(
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Batch size for scanning candidate nodes",
            min=1,
            max=5000,
        ),
    ] = 250,
    max_entities: Annotated[
        int,
        typer.Option(
            "--max-entities",
            help="Safety cap for maximum nodes scanned",
            min=1,
            max=1_000_000,
        ),
    ] = 20_000,
) -> None:
    """Fix legacy list-typed embeddings for FalkorDB vector search.

    Some older writes stored `name_embedding` as a plain List[float] instead of
    a Vectorf32 value. FalkorDB vector functions require Vectorf32, so this
    migration recasts `name_embedding` via `vecf32()`.
    """

    @run_async
    async def _fix() -> None:
        from sibyl.tools.admin import migrate_fix_name_embedding_types

        try:
            warn("Running embedding repair migration (this mutates graph data)")

            with spinner("Fixing embeddings...") as progress:
                task = progress.add_task("Casting name_embedding to Vectorf32...", total=None)
                result = await migrate_fix_name_embedding_types(
                    batch_size=batch_size,
                    max_entities=max_entities,
                )
                progress.update(task, description="Embedding repair complete")

            if result.success:
                success(result.message)
                info(f"Duration: {result.duration_seconds:.2f}s")
            else:
                error(f"Embedding repair failed: {result.message}")

        except Exception as e:
            error(f"Embedding repair failed: {e}")
            print_db_hint()

    _fix()


@app.command("backfill-task-relationships")
def backfill_task_relationships(
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for multi-tenant graph)"),
    ] = "",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview what would be done without making changes"),
    ] = False,
) -> None:
    """Backfill missing BELONGS_TO relationships between tasks and projects.

    Finds tasks with project_id in metadata but no BELONGS_TO edge to that project,
    and creates the missing relationship edges.

    Use --dry-run to preview what would be created without making changes.
    """
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

    @run_async
    async def _backfill() -> None:
        from sibyl.tools.admin import backfill_task_project_relationships

        try:
            if dry_run:
                warn("DRY RUN - no changes will be made")

            with spinner("Backfilling task relationships...") as progress:
                progress.add_task("Processing tasks...", total=None)
                result = await backfill_task_project_relationships(
                    organization_id=org_id,
                    dry_run=dry_run,
                )

            if result.success:
                if dry_run:
                    info(f"Would create {result.relationships_created} BELONGS_TO relationships")
                else:
                    success(f"Created {result.relationships_created} BELONGS_TO relationships")
            else:
                warn("Backfill completed with errors")

            info(f"Tasks without project_id: {result.tasks_without_project}")
            info(f"Tasks already linked: {result.tasks_already_linked}")
            info(f"Duration: {result.duration_seconds:.2f}s")

            if result.errors:
                warn(f"Errors: {len(result.errors)}")
                for err in result.errors[:5]:
                    console.print(f"  [dim]{err}[/dim]")
                if len(result.errors) > 5:
                    console.print(f"  [dim]...and {len(result.errors) - 5} more[/dim]")

        except Exception as e:
            error(f"Backfill failed: {e}")
            print_db_hint()

    _backfill()
