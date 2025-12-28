"""Database operations CLI commands.

Commands for backup, restore, and database management.
"""

import json
import subprocess
from datetime import UTC, datetime
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


# =============================================================================
# PostgreSQL Backup/Restore Commands
# =============================================================================


def _get_pg_env() -> dict[str, str]:
    """Get environment variables for pg_dump/psql commands."""
    import os

    from sibyl.config import settings

    env = os.environ.copy()
    env["PGPASSWORD"] = settings.postgres_password.get_secret_value()
    return env


def _get_pg_connection_args() -> list[str]:
    """Get common pg_dump/psql connection arguments."""
    from sibyl.config import settings

    return [
        "-h",
        settings.postgres_host,
        "-p",
        str(settings.postgres_port),
        "-U",
        settings.postgres_user,
        "-d",
        settings.postgres_db,
    ]


def _find_pg_tool(tool: str) -> str:
    """Find PostgreSQL tool (pg_dump/psql) preferring newer versions.

    Searches in order:
    1. Homebrew keg paths for PostgreSQL 18, 17, 16
    2. Standard PATH lookup
    """
    import shutil

    # Homebrew keg paths to check (prefer newer versions)
    keg_paths = [
        f"/opt/homebrew/opt/postgresql@18/bin/{tool}",
        f"/opt/homebrew/opt/postgresql@17/bin/{tool}",
        f"/opt/homebrew/opt/postgresql@16/bin/{tool}",
        f"/usr/local/opt/postgresql@18/bin/{tool}",
        f"/usr/local/opt/postgresql@17/bin/{tool}",
        f"/usr/local/opt/postgresql@16/bin/{tool}",
    ]

    for path in keg_paths:
        if Path(path).exists():
            return path

    # Fall back to PATH lookup
    found = shutil.which(tool)
    if found:
        return found

    return tool  # Return bare name, will fail with FileNotFoundError


@app.command("pg-backup")
def pg_backup(
    output: Annotated[Path, typer.Option("--output", "-o", help="Output SQL file path")] = Path(
        "sibyl_pg_backup.sql"
    ),
    data_only: Annotated[
        bool,
        typer.Option("--data-only", help="Backup data only (no schema)"),
    ] = False,
    schema_only: Annotated[
        bool,
        typer.Option("--schema-only", help="Backup schema only (no data)"),
    ] = False,
) -> None:
    """Backup PostgreSQL database using pg_dump.

    Creates a SQL dump that can be restored with pg-restore or psql.
    Includes all tables: users, organizations, api_keys, crawl_sources, etc.
    """
    if data_only and schema_only:
        error("Cannot use --data-only and --schema-only together")
        raise typer.Exit(code=1)

    try:
        from sibyl.config import settings

        info(
            f"Backing up PostgreSQL: {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
        )

        cmd = [
            _find_pg_tool("pg_dump"),
            *_get_pg_connection_args(),
            "--format=plain",
            "--no-owner",
            "--no-acl",
        ]

        if data_only:
            cmd.append("--data-only")
        elif schema_only:
            cmd.append("--schema-only")

        with spinner("Running pg_dump...") as progress:
            progress.add_task("Running pg_dump...", total=None)
            result = subprocess.run(  # noqa: S603 - trusted command
                cmd,
                env=_get_pg_env(),
                capture_output=True,
                text=True,
                check=False,
            )

        if result.returncode != 0:
            error(f"pg_dump failed: {result.stderr}")
            raise typer.Exit(code=1)

        # Write output
        output.write_text(result.stdout, encoding="utf-8")

        # Get file size
        size_kb = output.stat().st_size / 1024
        success(f"PostgreSQL backup created: {output} ({size_kb:.1f} KB)")

    except FileNotFoundError:
        error("pg_dump not found. Install PostgreSQL client tools.")
        raise typer.Exit(code=1) from None
    except Exception as e:
        error(f"Backup failed: {e}")
        raise typer.Exit(code=1) from None


@app.command("pg-restore")
def pg_restore(
    backup_file: Annotated[Path, typer.Argument(help="SQL backup file to restore")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Drop existing objects before restore (DANGEROUS)"),
    ] = False,
) -> None:
    """Restore PostgreSQL database from a SQL backup.

    WARNING: With --clean, this will DROP all existing data!
    """
    if not backup_file.exists():
        error(f"Backup file not found: {backup_file}")
        raise typer.Exit(code=1)

    if not yes:
        if clean:
            console.print(
                f"\n[{ERROR_RED}]WARNING: --clean will DROP ALL EXISTING DATA![/{ERROR_RED}]\n"
            )
            confirm = typer.confirm("Are you absolutely sure?")
            if not confirm:
                info("Cancelled")
                return
        else:
            warn("This will restore data from the backup file.")
            confirm = typer.confirm("Continue?")
            if not confirm:
                info("Cancelled")
                return

    try:
        from sibyl.config import settings

        info(
            f"Restoring to PostgreSQL: {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
        )

        # Read backup file
        sql_content = backup_file.read_text(encoding="utf-8")

        # If clean mode, add DROP statements
        if clean:
            # Get tables in reverse dependency order for clean drops
            drop_sql = """
-- Drop all tables in dependency order
DO $$ DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END $$;

-- Drop alembic version table too
DROP TABLE IF EXISTS alembic_version CASCADE;

"""
            sql_content = drop_sql + sql_content

        cmd = [
            _find_pg_tool("psql"),
            *_get_pg_connection_args(),
            "--quiet",
            "--set",
            "ON_ERROR_STOP=1",
        ]

        with spinner("Running psql restore...") as progress:
            progress.add_task("Running psql restore...", total=None)
            result = subprocess.run(  # noqa: S603 - trusted psql command
                cmd,
                env=_get_pg_env(),
                input=sql_content,
                capture_output=True,
                text=True,
                check=False,
            )

        if result.returncode != 0:
            error(f"psql restore failed: {result.stderr}")
            if "already exists" in result.stderr:
                info("Hint: Use --clean to drop existing tables before restore")
            raise typer.Exit(code=1)

        success("PostgreSQL restore complete!")

    except FileNotFoundError:
        error("psql not found. Install PostgreSQL client tools.")
        raise typer.Exit(code=1) from None
    except Exception as e:
        error(f"Restore failed: {e}")
        raise typer.Exit(code=1) from None


# =============================================================================
# Unified Backup/Restore (PostgreSQL + Graph)
# =============================================================================


@app.command("backup-all")
def backup_all(
    output_dir: Annotated[
        Path, typer.Option("--output-dir", "-o", help="Output directory for backup files")
    ] = Path("."),
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for graph backup)"),
    ] = "",
    prefix: Annotated[
        str,
        typer.Option("--prefix", help="Filename prefix for backup files"),
    ] = "",
) -> None:
    """Backup BOTH PostgreSQL database AND FalkorDB graph.

    Creates two files:
    - {prefix}sibyl_pg_backup.sql - PostgreSQL dump
    - {prefix}sibyl_graph_backup.json - FalkorDB graph (if org_id provided)

    This is the recommended backup command for full disaster recovery.
    """
    if not org_id:
        warn("--org-id not provided. Only PostgreSQL will be backed up.")
        warn("Graph backup requires an organization ID.")

    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    file_prefix = f"{prefix}{timestamp}_" if prefix else f"{timestamp}_"

    pg_file = output_dir / f"{file_prefix}sibyl_pg.sql"
    graph_file = output_dir / f"{file_prefix}sibyl_graph.json"

    # Backup PostgreSQL
    info("Step 1/2: Backing up PostgreSQL...")
    try:
        cmd = [
            _find_pg_tool("pg_dump"),
            *_get_pg_connection_args(),
            "--format=plain",
            "--no-owner",
            "--no-acl",
        ]

        result = subprocess.run(  # noqa: S603 - trusted pg_dump command
            cmd,
            env=_get_pg_env(),
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            error(f"PostgreSQL backup failed: {result.stderr}")
            raise typer.Exit(code=1)

        pg_file.write_text(result.stdout, encoding="utf-8")
        pg_size = pg_file.stat().st_size / 1024
        success(f"  PostgreSQL: {pg_file} ({pg_size:.1f} KB)")

    except FileNotFoundError:
        error("pg_dump not found. Install PostgreSQL client tools.")
        raise typer.Exit(code=1) from None

    # Backup Graph (if org_id provided)
    if org_id:
        info("Step 2/2: Backing up FalkorDB graph...")

        @run_async
        async def _backup_graph() -> bool:
            from dataclasses import asdict

            from sibyl.tools.admin import create_backup

            try:
                result = await create_backup(organization_id=org_id)

                if not result.success or result.backup_data is None:
                    error(f"  Graph backup failed: {result.message}")
                    return False

                backup_dict = asdict(result.backup_data)
                graph_file.write_text(
                    json.dumps(backup_dict, indent=2, default=str), encoding="utf-8"
                )

                graph_size = graph_file.stat().st_size / 1024
                success(
                    f"  FalkorDB: {graph_file} ({graph_size:.1f} KB) - "
                    f"{result.entity_count} entities, {result.relationship_count} relationships"
                )
                return True

            except Exception as e:
                error(f"  Graph backup failed: {e}")
                return False

        if not _backup_graph():
            warn("Graph backup failed, but PostgreSQL backup succeeded.")
    else:
        info("Step 2/2: Skipping graph backup (no --org-id)")

    console.print()
    success("Backup complete!")
    info(f"Files saved to: {output_dir.absolute()}")


def _find_backup_file(backup_dir: Path, explicit: str, patterns: list[str]) -> Path | None:
    """Find the most recent backup file matching given patterns."""
    if explicit:
        return backup_dir / explicit
    for pattern in patterns:
        files = sorted(backup_dir.glob(pattern), reverse=True)
        if files:
            return files[0]
    return None


def _restore_pg(pg_path: Path, clean: bool) -> None:
    """Restore PostgreSQL from backup file."""
    sql_content = pg_path.read_text(encoding="utf-8")

    if clean:
        drop_sql = """
DO $$ DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END $$;
DROP TABLE IF EXISTS alembic_version CASCADE;

"""
        sql_content = drop_sql + sql_content

    cmd = [_find_pg_tool("psql"), *_get_pg_connection_args(), "--quiet", "--set", "ON_ERROR_STOP=1"]

    result = subprocess.run(  # noqa: S603 - trusted psql command
        cmd, env=_get_pg_env(), input=sql_content, capture_output=True, text=True, check=False
    )

    if result.returncode != 0:
        error(f"  PostgreSQL restore failed: {result.stderr}")
        raise typer.Exit(code=1)

    success("  PostgreSQL restored!")


def _restore_graph_from_file(graph_path: Path, org_id: str, clean: bool) -> None:
    """Restore FalkorDB graph from backup file."""

    @run_async
    async def _restore() -> bool:
        from sibyl.tools.admin import BackupData, restore_backup

        try:
            backup_dict = json.loads(graph_path.read_text(encoding="utf-8"))

            backup_data = BackupData(
                version=backup_dict.get("version", "1.0"),
                created_at=backup_dict.get("created_at", ""),
                organization_id=backup_dict.get("organization_id", org_id),
                entity_count=backup_dict.get("entity_count", 0),
                relationship_count=backup_dict.get("relationship_count", 0),
                entities=backup_dict.get("entities", []),
                relationships=backup_dict.get("relationships", []),
            )

            result = await restore_backup(
                backup_data, organization_id=org_id, skip_existing=not clean
            )

            if result.success:
                success(
                    f"  FalkorDB restored: {result.entities_restored} entities, "
                    f"{result.relationships_restored} relationships"
                )
            else:
                warn(f"  Graph restore completed with errors: {len(result.errors)}")

            return result.success
        except Exception as e:
            error(f"  Graph restore failed: {e}")
            return False

    _restore()


@app.command("restore-all")
def restore_all(
    backup_dir: Annotated[Path, typer.Argument(help="Directory containing backup files")],
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for graph restore)"),
    ] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Drop existing data before restore (DANGEROUS)"),
    ] = False,
    pg_file: Annotated[
        str,
        typer.Option(
            "--pg-file", help="Specific PostgreSQL backup file (default: latest *_pg.sql)"
        ),
    ] = "",
    graph_file: Annotated[
        str,
        typer.Option(
            "--graph-file", help="Specific graph backup file (default: latest *_graph.json)"
        ),
    ] = "",
) -> None:
    """Restore BOTH PostgreSQL database AND FalkorDB graph from backup.

    Expects backup files created by 'backup-all' command.

    WARNING: With --clean, this will DROP ALL EXISTING DATA!
    """
    if not backup_dir.exists():
        error(f"Backup directory not found: {backup_dir}")
        raise typer.Exit(code=1)

    # Find backup files
    pg_path = _find_backup_file(backup_dir, pg_file, ["*_pg.sql", "*pg_backup.sql"])
    graph_path = _find_backup_file(backup_dir, graph_file, ["*_graph.json", "*graph_backup.json"])

    if not pg_path or not pg_path.exists():
        error("No PostgreSQL backup file found in directory")
        raise typer.Exit(code=1)

    info(f"PostgreSQL backup: {pg_path}")
    if graph_path and graph_path.exists():
        info(f"Graph backup: {graph_path}")
    else:
        warn("No graph backup file found. Only PostgreSQL will be restored.")
        graph_path = None

    # Confirmation
    if not yes:
        if clean:
            console.print(
                f"\n[{ERROR_RED}]WARNING: --clean will DROP ALL EXISTING DATA![/{ERROR_RED}]\n"
            )
            if not typer.confirm("Are you absolutely sure?"):
                info("Cancelled")
                return
        else:
            warn("This will restore data from the backup files.")
            if not typer.confirm("Continue?"):
                info("Cancelled")
                return

    # Restore PostgreSQL
    info("Step 1/2: Restoring PostgreSQL...")
    try:
        _restore_pg(pg_path, clean)
    except FileNotFoundError:
        error("psql not found. Install PostgreSQL client tools.")
        raise typer.Exit(code=1) from None

    # Restore Graph (if file exists and org_id provided)
    if graph_path and org_id:
        info("Step 2/2: Restoring FalkorDB graph...")
        _restore_graph_from_file(graph_path, org_id, clean)
    elif graph_path:
        warn("Step 2/2: Skipping graph restore (no --org-id provided)")
    else:
        info("Step 2/2: Skipping graph restore (no backup file)")

    console.print()
    success("Restore complete!")


@app.command("init-schema")
def init_schema(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Initialize database schema using Alembic migrations.

    Runs 'alembic upgrade head' to create all tables.
    Safe to run on empty database or to apply pending migrations.
    """
    if not yes:
        info("This will run Alembic migrations to create/update the schema.")
        confirm = typer.confirm("Continue?")
        if not confirm:
            info("Cancelled")
            return

    try:
        import os

        # Find alembic.ini
        project_root = Path(__file__).parent.parent.parent.parent
        alembic_ini = project_root / "alembic.ini"

        if not alembic_ini.exists():
            error(f"alembic.ini not found at {alembic_ini}")
            raise typer.Exit(code=1)

        with spinner("Running migrations...") as progress:
            progress.add_task("Running migrations...", total=None)

            result = subprocess.run(
                ["uv", "run", "alembic", "upgrade", "head"],  # noqa: S607
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
                env=os.environ,
            )

        if result.returncode != 0:
            error(f"Migration failed: {result.stderr}")
            if result.stdout:
                console.print(f"[dim]{result.stdout}[/dim]")
            raise typer.Exit(code=1)

        success("Schema initialized!")
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    info(f"  {line.strip()}")

    except Exception as e:
        error(f"Schema initialization failed: {e}")
        raise typer.Exit(code=1) from None
