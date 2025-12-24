"""Data export CLI commands.

Export graph data to JSON/CSV files.
"""

import json
from pathlib import Path
from typing import Annotated

import typer

from sibyl.cli.common import (
    error,
    info,
    print_db_hint,
    run_async,
    spinner,
    success,
)

app = typer.Typer(
    name="export",
    help="Export data to files (JSON/CSV)",
    no_args_is_help=True,
)


@app.command("graph")
def export_graph(
    output: Annotated[Path, typer.Option("--output", "-o", help="Output file path")] = Path(
        "sibyl_graph.json"
    ),
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for multi-tenant graph)"),
    ] = "",
) -> None:
    """Export the full graph to JSON."""
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

    @run_async
    async def _export() -> None:
        from sibyl.graph.client import get_graph_client
        from sibyl.graph.entities import EntityManager
        from sibyl.graph.relationships import RelationshipManager

        try:
            with spinner("Exporting graph...") as progress:
                task = progress.add_task("Exporting graph...", total=None)

                client = await get_graph_client()
                entity_mgr = EntityManager(client, group_id=org_id)
                rel_mgr = RelationshipManager(client, group_id=org_id)

                # Get all entities
                progress.update(task, description="Loading entities...")
                entities = []
                for entity_type in ["pattern", "rule", "template", "task", "project", "episode"]:
                    type_entities = await entity_mgr.list_by_type(entity_type, limit=1000)
                    entities.extend(type_entities)

                # Get all relationships
                progress.update(task, description="Loading relationships...")
                relationships = await rel_mgr.list_all(limit=5000)

                # Build export data
                export_data = {
                    "metadata": {
                        "exported_at": str(
                            __import__("datetime").datetime.now(
                                tz=__import__("datetime").timezone.utc
                            )
                        ),
                        "entity_count": len(entities),
                        "relationship_count": len(relationships),
                    },
                    "entities": [e.model_dump() for e in entities],
                    "relationships": [r.model_dump() for r in relationships],
                }

            # Write to file (sync I/O after async work)
            with open(output, "w") as f:  # noqa: ASYNC230
                json.dump(export_data, f, indent=2, default=str)

            success(f"Graph exported to {output}")
            info(f"Entities: {len(entities)}, Relationships: {len(relationships)}")

        except Exception as e:
            error(f"Export failed: {e}")
            print_db_hint()

    _export()


@app.command("tasks")
def export_tasks(
    output: Annotated[Path, typer.Option("--output", "-o", help="Output file path")] = Path(
        "tasks.csv"
    ),
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Filter by project")
    ] = None,
    status: Annotated[str | None, typer.Option("--status", "-s", help="Filter by status")] = None,
    format_: Annotated[
        str, typer.Option("--format", "-f", help="Output format: json, csv")
    ] = "csv",
) -> None:
    """Export tasks to CSV or JSON."""

    @run_async
    async def _export() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner("Exporting tasks...") as progress:
                progress.add_task("Exporting tasks...", total=None)
                response = await explore(
                    mode="list",
                    types=["task"],
                    project=project,
                    status=status,
                    limit=1000,
                )

            entities = response.entities or []

            if not entities:
                info("No tasks to export")
                return

            if format_ == "json":
                output_path = output.with_suffix(".json")
                with open(output_path, "w") as f:  # noqa: ASYNC230
                    json.dump([e.model_dump() for e in entities], f, indent=2, default=str)
            else:
                import csv

                output_path = output.with_suffix(".csv")
                with open(output_path, "w", newline="") as f:  # noqa: ASYNC230
                    writer = csv.writer(f)
                    writer.writerow(
                        [
                            "id",
                            "title",
                            "description",
                            "status",
                            "priority",
                            "project_id",
                            "feature",
                            "assignees",
                            "created_at",
                        ]
                    )
                    for e in entities:
                        meta = e.metadata or {}
                        writer.writerow(
                            [
                                e.id,
                                e.name,
                                e.description or "",
                                meta.get("status", ""),
                                meta.get("priority", ""),
                                meta.get("project_id", ""),
                                meta.get("feature", ""),
                                ",".join(meta.get("assignees", [])),
                                str(e.created_at) if e.created_at else "",
                            ]
                        )

            success(f"Exported {len(entities)} tasks to {output_path}")

        except Exception as e:
            error(f"Export failed: {e}")
            print_db_hint()

    _export()


@app.command("entities")
def export_entities(
    entity_type: Annotated[str, typer.Option("--type", "-t", help="Entity type to export")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Output file path")] = Path(
        "entities.json"
    ),
    format_: Annotated[
        str, typer.Option("--format", "-f", help="Output format: json, csv")
    ] = "json",
) -> None:
    """Export entities of a specific type."""

    @run_async
    async def _export() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner(f"Exporting {entity_type}s...") as progress:
                progress.add_task(f"Exporting {entity_type}s...", total=None)
                response = await explore(
                    mode="list",
                    types=[entity_type],
                    limit=1000,
                )

            entities = response.entities or []

            if not entities:
                info(f"No {entity_type}s to export")
                return

            if format_ == "json":
                output_path = output.with_suffix(".json")
                with open(output_path, "w") as f:  # noqa: ASYNC230
                    json.dump([e.model_dump() for e in entities], f, indent=2, default=str)
            else:
                import csv

                output_path = output.with_suffix(".csv")
                with open(output_path, "w", newline="") as f:  # noqa: ASYNC230
                    writer = csv.writer(f)
                    writer.writerow(["id", "name", "type", "description", "created_at"])
                    for e in entities:
                        writer.writerow(
                            [
                                e.id,
                                e.name,
                                e.type,
                                e.description or "",
                                str(e.created_at) if e.created_at else "",
                            ]
                        )

            success(f"Exported {len(entities)} {entity_type}(s) to {output_path}")

        except Exception as e:
            error(f"Export failed: {e}")
            print_db_hint()

    _export()
