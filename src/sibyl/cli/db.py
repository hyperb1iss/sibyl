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
) -> None:
    """Backup the graph database to a JSON file."""

    @run_async
    async def _backup() -> None:
        from sibyl.graph.entities import EntityManager
        from sibyl.graph.relationships import RelationshipManager

        try:
            with spinner("Creating backup...") as progress:
                task = progress.add_task("Creating backup...", total=None)

                from sibyl.graph.client import get_graph_client

                client = await get_graph_client()
                entity_mgr = EntityManager(client)
                rel_mgr = RelationshipManager(client)

                # Get all entity types
                progress.update(task, description="Backing up entities...")
                all_entities = []
                entity_types = [
                    "pattern",
                    "rule",
                    "template",
                    "tool",
                    "language",
                    "topic",
                    "episode",
                    "task",
                    "project",
                    "team",
                    "source",
                    "document",
                ]
                for etype in entity_types:
                    entities = await entity_mgr.list_by_type(etype, limit=5000)
                    all_entities.extend(entities)

                # Get all relationships
                progress.update(task, description="Backing up relationships...")
                relationships = await rel_mgr.list_all(limit=10000)

                # Build backup data
                backup_data = {
                    "version": "1.0",
                    "created_at": str(
                        __import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc)
                    ),
                    "entity_count": len(all_entities),
                    "relationship_count": len(relationships),
                    "entities": [e.model_dump() for e in all_entities],
                    "relationships": [r.model_dump() for r in relationships],
                }

            # Write backup (sync I/O after async work is done)
            with open(output, "w") as f:  # noqa: ASYNC230
                json.dump(backup_data, f, indent=2, default=str)

            success(f"Backup created: {output}")
            info(f"Entities: {len(all_entities)}, Relationships: {len(relationships)}")

        except Exception as e:
            error(f"Backup failed: {e}")
            print_db_hint()

    _backup()


@app.command("restore")
def restore_db(
    backup_file: Annotated[Path, typer.Argument(help="Backup file to restore")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Restore the database from a backup file."""
    if not backup_file.exists():
        error(f"Backup file not found: {backup_file}")
        return

    if not yes:
        warn("This will add entities from the backup to the database.")
        confirm = typer.confirm("Continue?")
        if not confirm:
            info("Cancelled")
            return

    @run_async
    async def _restore() -> None:
        from sibyl.graph.entities import EntityManager
        from sibyl.graph.relationships import RelationshipManager
        from sibyl.models.entities import Entity, Relationship

        try:
            # Load backup (sync I/O before async work)
            with open(backup_file) as f:  # noqa: ASYNC230
                backup_data = json.load(f)

            entity_count = backup_data.get("entity_count", 0)
            rel_count = backup_data.get("relationship_count", 0)

            info(f"Restoring {entity_count} entities and {rel_count} relationships...")

            with spinner("Restoring...") as progress:
                task = progress.add_task("Restoring...", total=None)

                from sibyl.graph.client import get_graph_client

                client = await get_graph_client()
                entity_mgr = EntityManager(client)
                rel_mgr = RelationshipManager(client)

                # Restore entities
                progress.update(task, description="Restoring entities...")
                restored_entities = 0
                for e_data in backup_data.get("entities", []):
                    try:
                        entity = Entity.model_validate(e_data)
                        await entity_mgr.create(entity)
                        restored_entities += 1
                    except Exception:  # noqa: S110
                        pass  # Skip duplicates or invalid entities

                # Restore relationships
                progress.update(task, description="Restoring relationships...")
                restored_rels = 0
                for r_data in backup_data.get("relationships", []):
                    try:
                        rel = Relationship.model_validate(r_data)
                        await rel_mgr.create(rel)
                        restored_rels += 1
                    except Exception:  # noqa: S110
                        pass  # Skip duplicates

            success("Restore complete!")
            info(f"Restored {restored_entities} entities, {restored_rels} relationships")

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
                await client.driver.execute_query("MATCH (n) DETACH DELETE n")

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
                node_result = await client.driver.execute_query(
                    "MATCH (n) RETURN count(n) as count"
                )
                node_count = node_result.result_set[0][0] if node_result.result_set else 0

                # Get relationship count
                rel_result = await client.driver.execute_query(
                    "MATCH ()-[r]->() RETURN count(r) as count"
                )
                rel_count = rel_result.result_set[0][0] if rel_result.result_set else 0

                # Get node types
                type_result = await client.driver.execute_query(
                    "MATCH (n) RETURN n.entity_type as type, count(*) as count ORDER BY count DESC"
                )

            console.print(f"\n[{NEON_CYAN}]Database Statistics[/{NEON_CYAN}]\n")
            console.print(f"  Total Nodes: {node_count}")
            console.print(f"  Total Relationships: {rel_count}")

            if type_result.result_set:
                console.print("\n  [dim]By Entity Type:[/dim]")
                for row in type_result.result_set:
                    if row[0]:
                        console.print(f"    {row[0]}: {row[1]}")

        except Exception as e:
            error(f"Failed to get stats: {e}")
            print_db_hint()

    _stats()


@app.command("migrate")
def db_migrate() -> None:
    """Run database migrations to fix schema issues.

    Currently supports:
    - Adding group_ids to entities for Graphiti compatibility
    """

    @run_async
    async def _migrate() -> None:
        from sibyl.tools.admin import migrate_add_group_ids

        try:
            info("Running database migrations...")

            with spinner("Migrating...") as progress:
                task = progress.add_task("Adding group_ids to entities...", total=None)

                result = await migrate_add_group_ids()

                progress.update(task, description="Migration complete")

            if result.success:
                success(f"Migration complete: {result.message}")
                info(f"Duration: {result.duration_seconds:.2f}s")
            else:
                error(f"Migration failed: {result.message}")

        except Exception as e:
            error(f"Migration failed: {e}")
            print_db_hint()

    _migrate()
