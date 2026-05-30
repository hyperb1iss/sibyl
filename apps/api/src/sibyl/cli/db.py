"""Database operations CLI commands.

Commands for backup, restore, and database management.
"""

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import typer

from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    ERROR_RED,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    error,
    info,
    print_db_hint,
    print_json,
    run_async,
    success,
    warn,
)

app = typer.Typer(
    name="db",
    help="Database operations",
    no_args_is_help=True,
)


def _first_count(rows: object) -> int:
    if not isinstance(rows, list) or not rows:
        return 0
    row = rows[0]
    if isinstance(row, dict):
        value = row.get("count") or row.get("deleted")
        return value if isinstance(value, int) else 0
    if isinstance(row, list | tuple) and row:
        value = row[0]
        return value if isinstance(value, int) else 0
    return 0


def _first_mapping(rows: object) -> dict[str, object]:
    if isinstance(rows, Mapping):
        return dict(rows)
    if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
        return dict(rows[0])
    return {}


def _normalize_rows(rows: object) -> list[dict[str, object]]:
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _extract_definitions(value: object) -> list[dict[str, str]]:
    if isinstance(value, Mapping):
        return [
            {"name": str(name), "definition": str(definition)}
            for name, definition in sorted(value.items(), key=lambda item: str(item[0]))
        ]
    if isinstance(value, list):
        definitions: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, Mapping):
                name = item.get("name") or item.get("id")
                definition = item.get("definition") or item.get("sql") or item
                definitions.append({"name": str(name or ""), "definition": str(definition)})
            else:
                definitions.append({"name": "", "definition": str(item)})
        return definitions
    return []


def _extract_table_names(db_info: Mapping[str, object]) -> list[str]:
    tables = db_info.get("tables")
    if isinstance(tables, Mapping):
        return sorted(str(table) for table in tables)
    return []


async def _safe_execute(
    client: Any, query: str, **params: object
) -> tuple[object | None, str | None]:
    try:
        return await client.execute_query(query, **params), None
    except Exception as exc:  # pragma: no cover - exercised through command behavior
        return None, str(exc)


async def _collect_table_inventory(client: Any, tables: tuple[str, ...]) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for table in tables:
        count_rows, count_error = await _safe_execute(
            client,
            f"SELECT count() AS count FROM {table} GROUP ALL;",  # noqa: S608
        )
        info_rows, info_error = await _safe_execute(
            client,
            f"INFO FOR TABLE {table};",
        )
        table_info = _first_mapping(info_rows)
        entry: dict[str, object] = {
            "name": table,
            "count": None if count_error else _first_count(count_rows),
            "indexes": _extract_definitions(table_info.get("indexes")),
        }
        if count_error:
            entry["count_error"] = count_error
        if info_error:
            entry["info_error"] = info_error
        inventory.append(entry)
    return inventory


async def _collect_schema_versions(client: Any) -> list[dict[str, object]]:
    rows, error_text = await _safe_execute(
        client,
        """
        SELECT name, version, embedding_dimension, migrations
        FROM schema_version
        ORDER BY name;
        """,
    )
    if error_text:
        return [{"error": error_text}]
    return _normalize_rows(rows)


async def _collect_plane_inventory(
    client: Any,
    *,
    tables: tuple[str, ...],
) -> dict[str, object]:
    db_info_rows, error_text = await _safe_execute(client, "INFO FOR DB;")
    db_info = _first_mapping(db_info_rows)
    inventory: dict[str, object] = {
        "tables": await _collect_table_inventory(client, tables),
        "schema_versions": await _collect_schema_versions(client),
        "defined_tables": _extract_table_names(db_info),
        "table_definitions": _extract_definitions(db_info.get("tables")),
    }
    if error_text:
        inventory["info_error"] = error_text
    return inventory


async def _collect_orphan_counts(
    client: Any,
    checks: tuple[tuple[str, str], ...],
    *,
    org_id: str,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for name, query in checks:
        rows, error_text = await _safe_execute(client, query, org_id=org_id)
        entry: dict[str, object] = {"name": name, "count": 0 if error_text else _first_count(rows)}
        if error_text:
            entry["error"] = error_text
        results.append(entry)
    return results


def _configured_vector_indexes() -> list[dict[str, object]]:
    from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM as CONTENT_EMBEDDING_DIM
    from sibyl_core.backends.surreal.schema import (
        EMBEDDING_DIM as GRAPH_EMBEDDING_DIM,
        EMBEDDING_VECTOR_FIELDS,
    )

    vectors: list[dict[str, object]] = [
        {
            "plane": "content",
            "table": "document_chunks",
            "field": "embedding",
            "index": "idx_document_chunks_embedding",
            "dimension": CONTENT_EMBEDDING_DIM,
            "definition": (
                "DEFINE INDEX idx_document_chunks_embedding ON document_chunks FIELDS embedding "
                f"HNSW DIMENSION {CONTENT_EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12"
            ),
        }
    ]
    vectors.extend(
        {
            "plane": "graph",
            "table": vector_field.table,
            "field": vector_field.field,
            "index": vector_field.index,
            "dimension": GRAPH_EMBEDDING_DIM,
            "definition": vector_field.index_definition(GRAPH_EMBEDDING_DIM).definition,
        }
        for vector_field in EMBEDDING_VECTOR_FIELDS
    )
    return vectors


def _zero_vector(dimensions: int) -> list[float]:
    return [0.0] * max(int(dimensions), 1)


def _split_probe_values(raw: str, *, fallback: str | None = None) -> tuple[str, ...]:
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if values:
        return values
    return (fallback,) if fallback is not None else ()


_AUTH_ORPHAN_CHECKS = (
    (
        "organization_members_missing_organization",
        """
        SELECT count() AS count FROM organization_members
        WHERE organization_id = $org_id
          AND organization_id NOT IN (SELECT VALUE uuid FROM organizations)
        GROUP ALL;
        """,
    ),
    (
        "organization_members_missing_user",
        """
        SELECT count() AS count FROM organization_members
        WHERE organization_id = $org_id
          AND user_id NOT IN (SELECT VALUE uuid FROM users)
        GROUP ALL;
        """,
    ),
    (
        "projects_missing_owner",
        """
        SELECT count() AS count FROM projects
        WHERE organization_id = $org_id
          AND owner_user_id != NONE
          AND owner_user_id NOT IN (SELECT VALUE uuid FROM users)
        GROUP ALL;
        """,
    ),
    (
        "project_members_missing_project",
        """
        SELECT count() AS count FROM project_members
        WHERE organization_id = $org_id
          AND project_id NOT IN (SELECT VALUE uuid FROM projects)
        GROUP ALL;
        """,
    ),
    (
        "project_members_missing_user",
        """
        SELECT count() AS count FROM project_members
        WHERE organization_id = $org_id
          AND user_id NOT IN (SELECT VALUE uuid FROM users)
        GROUP ALL;
        """,
    ),
)


_CONTENT_ORPHAN_CHECKS = (
    (
        "crawled_documents_missing_source",
        """
        SELECT count() AS count FROM crawled_documents
        WHERE source_id NOT IN (SELECT VALUE uuid FROM crawl_sources)
        GROUP ALL;
        """,
    ),
    (
        "document_chunks_missing_document",
        """
        SELECT count() AS count FROM document_chunks
        WHERE document_id NOT IN (SELECT VALUE uuid FROM crawled_documents)
        GROUP ALL;
        """,
    ),
    (
        "raw_captures_missing_source",
        """
        SELECT count() AS count FROM raw_captures
        WHERE organization_id = $org_id
          AND source_id != ''
          AND source_id NOT IN (SELECT VALUE uuid FROM crawl_sources)
        GROUP ALL;
        """,
    ),
)


_GRAPH_ORPHAN_CHECKS = (
    (
        "relates_to_missing_source",
        """
        SELECT count() AS count FROM relates_to
        WHERE group_id = $org_id
          AND in NOT IN (SELECT VALUE id FROM entity)
        GROUP ALL;
        """,
    ),
    (
        "relates_to_missing_target",
        """
        SELECT count() AS count FROM relates_to
        WHERE group_id = $org_id
          AND out NOT IN (SELECT VALUE id FROM entity)
        GROUP ALL;
        """,
    ),
    (
        "mentions_missing_episode",
        """
        SELECT count() AS count FROM mentions
        WHERE group_id = $org_id
          AND in NOT IN (SELECT VALUE id FROM episode)
        GROUP ALL;
        """,
    ),
    (
        "mentions_missing_entity",
        """
        SELECT count() AS count FROM mentions
        WHERE group_id = $org_id
          AND out NOT IN (SELECT VALUE id FROM entity)
        GROUP ALL;
        """,
    ),
)


async def collect_database_inventory(org_id: str) -> dict[str, object]:
    from sibyl.persistence.surreal.auth import build_surreal_auth_client
    from sibyl.persistence.surreal.content import build_surreal_content_client
    from sibyl_core.backends.surreal.auth_schema import AUTH_TABLES
    from sibyl_core.backends.surreal.content_schema import CONTENT_TABLES
    from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES
    from sibyl_core.services.graph import get_surreal_graph_client

    auth_client = build_surreal_auth_client()
    content_client = build_surreal_content_client()
    graph_client = None
    try:
        graph_client = await get_surreal_graph_client(org_id)
        return {
            "org_id": org_id,
            "auth": await _collect_plane_inventory(auth_client, tables=AUTH_TABLES),
            "content": await _collect_plane_inventory(content_client, tables=CONTENT_TABLES),
            "graph": await _collect_plane_inventory(
                graph_client,
                tables=(*GRAPH_TABLES, *GRAPH_EDGES),
            ),
            "orphans": {
                "auth": await _collect_orphan_counts(
                    auth_client,
                    _AUTH_ORPHAN_CHECKS,
                    org_id=org_id,
                ),
                "content": await _collect_orphan_counts(
                    content_client,
                    _CONTENT_ORPHAN_CHECKS,
                    org_id=org_id,
                ),
                "graph": await _collect_orphan_counts(
                    graph_client,
                    _GRAPH_ORPHAN_CHECKS,
                    org_id=org_id,
                ),
            },
            "vectors": _configured_vector_indexes(),
        }
    finally:
        for client in (auth_client, content_client, graph_client):
            if client is None:
                continue
            close = getattr(client, "close", None)
            if callable(close):
                await close()


async def collect_query_plan_probe_report(
    *,
    org_id: str,
    source_ids: tuple[str, ...],
    project_ids: tuple[str, ...],
    entity_types: tuple[str, ...],
    node_types: tuple[str, ...],
    query_text: str,
    limit: int,
    graph_embedding_dim: int,
    content_embedding_dim: int,
    max_executed_rows: int | None,
) -> dict[str, object]:
    from sibyl.persistence.surreal.content import build_surreal_content_client
    from sibyl_core.backends.surreal.query_plan_probes import (
        build_hot_query_plan_probes,
        run_query_plan_probes,
    )
    from sibyl_core.services.graph import get_surreal_graph_client

    content_client = build_surreal_content_client()
    graph_client = None
    try:
        graph_client = await get_surreal_graph_client(org_id)
        probes = list(
            build_hot_query_plan_probes(
                org_id=org_id,
                graph_query_embedding=_zero_vector(graph_embedding_dim),
                content_query_embedding=_zero_vector(content_embedding_dim),
                query_text=query_text,
                source_ids=source_ids,
                project_ids=project_ids,
                entity_types=entity_types,
                node_types=node_types,
                limit=limit,
            )
        )
        if max_executed_rows is not None:
            probes = [replace(probe, max_executed_rows=max_executed_rows) for probe in probes]
        results = await run_query_plan_probes(
            {"content": content_client, "graph": graph_client},
            probes,
        )
        return {
            "org_id": org_id,
            "source_ids": list(source_ids),
            "project_ids": list(project_ids),
            "query_text": query_text,
            "probes": [result.to_dict() for result in results],
        }
    finally:
        for client in (content_client, graph_client):
            if client is None:
                continue
            close = getattr(client, "close", None)
            if callable(close):
                await close()


async def _get_graph_client(org_id: str):
    from sibyl_core.services.graph import (
        get_surreal_graph_client,
        prepare_graph_schema,
    )

    client = await get_surreal_graph_client(org_id)
    await prepare_graph_schema(client)
    return client


async def _clear_native_group_data(client: Any, org_id: str) -> None:
    from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES

    for table in (*GRAPH_EDGES, *GRAPH_TABLES):
        await client.execute_query(
            f"DELETE FROM {table} WHERE group_id = $group_id;",  # noqa: S608
            group_id=org_id,
        )


def _coerce_graph_backup_data(payload: dict[str, object], org_id: str):
    """Normalize graph backup payloads from backup and export commands."""
    from sibyl_core.tools.admin import BackupData

    metadata = payload.get("metadata")
    metadata_dict = metadata if isinstance(metadata, dict) else {}

    raw_entities = payload.get("entities")
    entities = list(raw_entities) if isinstance(raw_entities, list) else []

    raw_relationships = payload.get("relationships")
    relationships = list(raw_relationships) if isinstance(raw_relationships, list) else []

    raw_episodes = payload.get("episodes")
    episodes = list(raw_episodes) if isinstance(raw_episodes, list) else []

    raw_mentions = payload.get("mentions")
    mentions = list(raw_mentions) if isinstance(raw_mentions, list) else []

    def _count(key: str, fallback: int) -> int:
        value = payload.get(key)
        if isinstance(value, int):
            return value
        meta_value = metadata_dict.get(key)
        if isinstance(meta_value, int):
            return meta_value
        return fallback

    created_at = payload.get("created_at")
    if not created_at:
        created_at = metadata_dict.get("exported_at", "")

    organization_id = payload.get("organization_id") or org_id

    return BackupData(
        version=str(payload.get("version") or "2.0"),
        created_at=str(created_at or ""),
        organization_id=str(organization_id or org_id),
        entity_count=_count("entity_count", len(entities)),
        relationship_count=_count("relationship_count", len(relationships)),
        entities=entities,
        relationships=relationships,
        episode_count=_count("episode_count", len(episodes)),
        mention_count=_count("mention_count", len(mentions)),
        episodes=episodes,
        mentions=mentions,
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

        from sibyl_core.tools.admin import create_backup

        try:
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
    backup_file: Annotated[Path, typer.Argument(help="Backup or graph export file to restore")],
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
    """Restore the graph runtime from a backup or graph export file."""
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
        from sibyl_core.tools.admin import restore_backup

        try:
            # Load backup file (sync I/O before async work)
            with open(backup_file) as f:  # noqa: ASYNC230
                backup_dict = json.load(f)

            backup_data = _coerce_graph_backup_data(backup_dict, org_id)

            info(
                "Restoring "
                f"{backup_data.entity_count} entities, "
                f"{backup_data.relationship_count} relationships, "
                f"{backup_data.episode_count} episodes, "
                f"and {backup_data.mention_count} mentions..."
            )
            await _prepare_graph_runtime_async(org_id, clean=False)

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
                "Restored: "
                f"{result.entities_restored} entities, "
                f"{result.relationships_restored} relationships, "
                f"{getattr(result, 'episodes_restored', 0)} episodes, "
                f"{getattr(result, 'mentions_restored', 0)} mentions"
            )
            if (
                result.entities_skipped
                or result.relationships_skipped
                or getattr(result, "episodes_skipped", 0)
                or getattr(result, "mentions_skipped", 0)
            ):
                info(
                    "Skipped: "
                    f"{result.entities_skipped} entities, "
                    f"{result.relationships_skipped} relationships, "
                    f"{getattr(result, 'episodes_skipped', 0)} episodes, "
                    f"{getattr(result, 'mentions_skipped', 0)} mentions"
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
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for graph operations)"),
    ] = "",
) -> None:
    """Clear all data from the database. USE WITH CAUTION!"""
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

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
        try:
            client = await _get_graph_client(org_id)
            await _clear_native_group_data(client, org_id)

            success("Database cleared")
            warn("All data has been deleted")

        except Exception as e:
            error(f"Clear failed: {e}")
            print_db_hint()

    _clear()


@app.command("stats")
def db_stats(
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for graph operations)"),
    ] = "",
) -> None:
    """Show detailed database statistics."""
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

    @run_async
    async def _stats() -> None:
        from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES

        try:
            client = await _get_graph_client(org_id)

            node_count = 0
            rel_count = 0
            for table in GRAPH_TABLES:
                rows = await client.execute_query(
                    f"SELECT count() AS count FROM {table} WHERE group_id = $group_id GROUP ALL;",  # noqa: S608
                    group_id=org_id,
                )
                node_count += _first_count(rows)
            for table in GRAPH_EDGES:
                rows = await client.execute_query(
                    f"SELECT count() AS count FROM {table} WHERE group_id = $group_id GROUP ALL;",  # noqa: S608
                    group_id=org_id,
                )
                rel_count += _first_count(rows)
            type_rows = await client.execute_query(
                """
                SELECT entity_type AS type, count() AS count
                FROM entity
                WHERE group_id = $group_id
                GROUP BY entity_type
                ORDER BY count DESC;
                """,
                group_id=org_id,
            )

            console.print(f"\n[{NEON_CYAN}]Database Statistics[/{NEON_CYAN}]\n")
            console.print(f"  Total Nodes: {node_count}")
            console.print(f"  Total Relationships: {rel_count}")

            if type_rows:
                console.print("\n  [dim]By Entity Type:[/dim]")
                for row in type_rows:
                    if isinstance(row, dict):
                        row_type = row.get("type")
                        row_count = row.get("count")
                    else:
                        row_type = row[0] if row else None
                        row_count = row[1] if len(row) > 1 else 0
                    if row_type:
                        console.print(f"    {row_type}: {row_count}")

        except Exception as e:
            error(f"Failed to get stats: {e}")
            print_db_hint()

    _stats()


@app.command("inventory")
def db_inventory(
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID for graph and scoped checks"),
    ] = "",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the inventory as JSON"),
    ] = False,
) -> None:
    """Collect a read-only schema and data inventory across Surreal stores."""
    if not org_id:
        error("--org-id is required for inventory")
        raise typer.Exit(code=1)

    @run_async
    async def _inventory() -> None:
        try:
            inventory = await collect_database_inventory(org_id)
            if json_output:
                print_json(inventory)
                return

            console.print(f"\n[{NEON_CYAN}]Database Inventory[/{NEON_CYAN}]\n")
            console.print(f"  Org: [{ELECTRIC_PURPLE}]{org_id}[/{ELECTRIC_PURPLE}]")
            for plane in ("auth", "content", "graph"):
                plane_inventory = inventory.get(plane)
                if not isinstance(plane_inventory, Mapping):
                    continue
                tables = plane_inventory.get("tables")
                table_count = len(tables) if isinstance(tables, list) else 0
                schema_versions = plane_inventory.get("schema_versions")
                console.print(f"\n  [{NEON_CYAN}]{plane}[/{NEON_CYAN}]")
                console.print(f"    Tables: {table_count}")
                if isinstance(schema_versions, list) and schema_versions:
                    console.print("    Schema versions:")
                    for row in schema_versions:
                        if isinstance(row, Mapping):
                            name = row.get("name") or plane
                            version = row.get("version", "unknown")
                            console.print(f"      {name}: v{version}")

            orphans = inventory.get("orphans")
            if isinstance(orphans, Mapping):
                console.print("\n  [dim]Orphan checks:[/dim]")
                for plane, checks in orphans.items():
                    if isinstance(checks, list):
                        total = sum(
                            int(check.get("count", 0))
                            for check in checks
                            if isinstance(check, Mapping) and isinstance(check.get("count"), int)
                        )
                        console.print(f"    {plane}: {total}")

            vectors = inventory.get("vectors")
            if isinstance(vectors, list):
                console.print("\n  [dim]Vector indexes:[/dim]")
                for vector in vectors:
                    if isinstance(vector, Mapping):
                        console.print(
                            "    "
                            f"{vector.get('plane')}.{vector.get('table')}.{vector.get('field')}: "
                            f"{vector.get('dimension')}d"
                        )
        except Exception as e:
            error(f"Inventory failed: {e}")
            print_db_hint()

    _inventory()


@app.command("plan-probes")
def db_plan_probes(
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID for graph and scoped checks"),
    ] = "",
    source_ids: Annotated[
        str,
        typer.Option(
            "--source-id",
            help="Comma-separated content source UUIDs or names used by content probes",
        ),
    ] = "",
    project_ids: Annotated[
        str,
        typer.Option("--project-id", help="Comma-separated graph project IDs used by probes"),
    ] = "",
    entity_types: Annotated[
        str,
        typer.Option("--entity-type", help="Comma-separated entity types for entity search"),
    ] = "",
    node_types: Annotated[
        str,
        typer.Option("--node-type", help="Comma-separated node types for context search"),
    ] = "task",
    query_text: Annotated[
        str,
        typer.Option("--query", help="Lexical query text used by content probes"),
    ] = "sibyl query plan probe",
    limit: Annotated[
        int,
        typer.Option("--limit", help="Requested result limit for probe query construction", min=1),
    ] = 10,
    graph_embedding_dim: Annotated[
        int,
        typer.Option(
            "--graph-embedding-dim",
            help="Graph query vector dimensions; 0 uses the configured schema dimension",
            min=0,
        ),
    ] = 0,
    content_embedding_dim: Annotated[
        int,
        typer.Option(
            "--content-embedding-dim",
            help="Content query vector dimensions; 0 uses the configured schema dimension",
            min=0,
        ),
    ] = 0,
    max_executed_rows: Annotated[
        int,
        typer.Option(
            "--max-executed-rows",
            help="Optional threshold for EXPLAIN FULL row-count warnings; 0 disables it",
            min=0,
        ),
    ] = 0,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print probe results as JSON"),
    ] = False,
) -> None:
    """Run read-only EXPLAIN FULL probes for hot graph and content searches."""
    if not org_id:
        error("--org-id is required for plan probes")
        raise typer.Exit(code=1)

    @run_async
    async def _plan_probes() -> None:
        from sibyl_core.backends.surreal.content_schema import (
            EMBEDDING_DIM as CONTENT_EMBEDDING_DIM,
        )
        from sibyl_core.backends.surreal.schema import EMBEDDING_DIM as GRAPH_EMBEDDING_DIM

        try:
            report = await collect_query_plan_probe_report(
                org_id=org_id,
                source_ids=_split_probe_values(source_ids, fallback="probe-source"),
                project_ids=_split_probe_values(project_ids, fallback="probe-project"),
                entity_types=_split_probe_values(entity_types),
                node_types=_split_probe_values(node_types, fallback="task"),
                query_text=query_text,
                limit=limit,
                graph_embedding_dim=graph_embedding_dim or GRAPH_EMBEDDING_DIM,
                content_embedding_dim=content_embedding_dim or CONTENT_EMBEDDING_DIM,
                max_executed_rows=max_executed_rows or None,
            )
            if json_output:
                print_json(report)
                return

            console.print(f"\n[{NEON_CYAN}]Query Plan Probes[/{NEON_CYAN}]\n")
            console.print(f"  Org: [{ELECTRIC_PURPLE}]{org_id}[/{ELECTRIC_PURPLE}]")
            probes = report.get("probes")
            if not isinstance(probes, list):
                return
            for probe in probes:
                if not isinstance(probe, Mapping):
                    continue
                analysis = probe.get("analysis")
                error_text = probe.get("error")
                expected = probe.get("expected_indexes")
                if error_text:
                    console.print(
                        f"  [{ERROR_RED}]check[/{ERROR_RED}] {probe.get('name')}: {error_text}"
                    )
                    continue
                uses_expected = (
                    isinstance(analysis, Mapping) and analysis.get("uses_expected_index") is True
                )
                label = "ok" if uses_expected else "check"
                color = SUCCESS_GREEN if uses_expected else ERROR_RED
                console.print(f"  [{color}]{label}[/{color}] {probe.get('name')}")
                if expected:
                    console.print(f"      expected: {expected}")
                if isinstance(analysis, Mapping):
                    console.print(f"      used: {analysis.get('used_indexes')}")
                    scan_operations = analysis.get("scan_operations")
                    if scan_operations:
                        console.print(f"      scans: {scan_operations}")
        except Exception as e:
            error(f"Plan probes failed: {e}")
            print_db_hint()

    _plan_probes()


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
    """Run the legacy FalkorDB embedding repair command.

    Some older writes stored `name_embedding` as a plain List[float] instead of
    a Vectorf32 value. This migration is retained only for compatibility
    archives or preserved legacy source environments.
    """

    @run_async
    async def _fix() -> None:
        from sibyl_core.tools.admin import migrate_fix_name_embedding_types

        try:
            warn("Running embedding repair migration (this mutates graph data)")

            result = await migrate_fix_name_embedding_types(
                batch_size=batch_size,
                max_entities=max_entities,
            )

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
        from sibyl_core.tools.admin import backfill_task_project_relationships

        try:
            if dry_run:
                warn("DRY RUN - no changes will be made")

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


@app.command("backfill-project-ids")
def backfill_project_ids(
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for multi-tenant graph)"),
    ] = "",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview what would be done without making changes"),
    ] = False,
) -> None:
    """Backfill project_id property on nodes based on BELONGS_TO relationships.

    Finds nodes that have BELONGS_TO edges to projects but are missing the
    project_id property, and sets it based on the relationship target.

    This ensures the "Unassigned" filter in the graph view works correctly.

    Use --dry-run to preview what would be updated without making changes.
    """
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

    @run_async
    async def _backfill() -> None:
        from sibyl_core.tools.admin import backfill_project_id_from_relationships

        try:
            if dry_run:
                warn("DRY RUN - no changes will be made")

            result = await backfill_project_id_from_relationships(
                organization_id=org_id,
                dry_run=dry_run,
            )

            if result.success:
                if dry_run:
                    info(f"Would update {result.nodes_updated} nodes with project_id")
                else:
                    success(f"Updated {result.nodes_updated} nodes with project_id")
            else:
                warn("Backfill completed with errors")

            info(f"Nodes already have project_id: {result.nodes_already_set}")
            info(f"Nodes without any project relationship: {result.nodes_without_project_rel}")
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


@app.command("backfill-denormalized-fields")
def backfill_denormalized_fields_cmd(
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for multi-tenant graph)"),
    ] = "",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview what would be done without making changes"),
    ] = False,
) -> None:
    """Re-save entity rows so denormalized fields match metadata.

    Older task/epic rows have filter fields (project_id, epic_id, status, etc.)
    only inside attributes.metadata. The optimized SurrealDB list query filters
    on top-level columns, so those rows never show up in `sibyl task list` until
    they get touched. This walks every entity in the org and re-saves any row
    where metadata has a denormalized field that the row column is missing.

    Use --dry-run to preview without making changes.
    """
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

    @run_async
    async def _backfill() -> None:
        from sibyl_core.tools.admin import backfill_denormalized_fields

        try:
            if dry_run:
                warn("DRY RUN - no changes will be made")

            result = await backfill_denormalized_fields(
                organization_id=org_id,
                dry_run=dry_run,
            )

            if result.success:
                if dry_run:
                    info(f"Would re-save {result.entities_updated} entities")
                else:
                    success(f"Re-saved {result.entities_updated} entities")
            else:
                warn("Backfill completed with errors")

            info(f"Entities scanned: {result.entities_scanned}")
            info(f"Entities already denormalized: {result.entities_already_set}")
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


@app.command("backfill-episode-relationships")
def backfill_episode_relationships(
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID (required for multi-tenant graph)"),
    ] = "",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview what would be done without making changes"),
    ] = False,
) -> None:
    """Backfill RELATED_TO relationships from episodes to their referenced tasks.

    Finds episode nodes that have task_id in metadata but no relationship edge
    to that task, and creates RELATED_TO edges.

    This ensures episode nodes appear connected to their tasks in the graph view.

    Use --dry-run to preview what would be created without making changes.
    """
    if not org_id:
        error("--org-id is required for graph operations")
        raise typer.Exit(code=1)

    @run_async
    async def _backfill() -> None:
        from sibyl_core.tools.admin import backfill_episode_task_relationships

        try:
            if dry_run:
                warn("DRY RUN - no changes will be made")

            result = await backfill_episode_task_relationships(
                organization_id=org_id,
                dry_run=dry_run,
            )

            if result.success:
                if dry_run:
                    info(f"Would create {result.relationships_created} RELATED_TO relationships")
                else:
                    success(f"Created {result.relationships_created} RELATED_TO relationships")
            else:
                warn("Backfill completed with errors")

            info(f"Episodes already linked: {result.episodes_already_linked}")
            info(f"Episodes without valid task: {result.episodes_without_task}")
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
# Graph runtime restore helpers
# =============================================================================


async def _prepare_graph_runtime_async(org_id: str, *, clean: bool) -> None:
    """Ensure the target graph runtime is ready for restore."""
    from sibyl_core.backends.surreal.schema import bootstrap_schema
    from sibyl_core.services.graph import get_surreal_graph_client

    client = await get_surreal_graph_client(org_id)

    # Bootstrap the SCHEMAFULL tables + indexes. With reset=True, this also
    # drops existing tables. With reset=False, IF NOT EXISTS lets it run
    # idempotently.
    await bootstrap_schema(client, reset=clean)

    if clean:
        await _clear_native_group_data(client, org_id)
        return


def _prepare_graph_runtime(org_id: str, *, clean: bool) -> None:
    """Ensure the target graph runtime is ready for restore."""

    @run_async
    async def _prepare() -> None:
        await _prepare_graph_runtime_async(org_id, clean=clean)

    _prepare()


# =============================================================================
# API-Based Backup Management
# =============================================================================


def _get_api_url() -> str:
    """Get API base URL from settings."""
    from sibyl.config import settings

    host = settings.server_host
    if host in {"0.0.0.0", "::"}:  # noqa: S104
        host = "localhost"
    return f"http://{host}:{settings.server_port}"


def _api_request(
    method: str,
    path: str,
    *,
    json_data: dict | None = None,
    stream: bool = False,
) -> dict | bytes:
    """Make an API request to the backup endpoints.

    Note: This requires the API server to be running and assumes local access.
    For production, you'd use proper auth headers.
    """
    import httpx

    url = f"{_get_api_url()}{path}"

    try:
        with httpx.Client(timeout=300) as client:  # 5 min timeout for backups
            if method == "GET":
                if stream:
                    response = client.get(url)
                    response.raise_for_status()
                    return response.content
                response = client.get(url)
            elif method == "POST":
                response = client.post(url, json=json_data or {})
            elif method == "DELETE":
                response = client.delete(url)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()

    except httpx.ConnectError:
        error("Cannot connect to Sibyl API. Is 'sibyld serve' running?")
        raise typer.Exit(code=1) from None
    except httpx.HTTPStatusError as e:
        error(f"API error: {e.response.status_code} - {e.response.text}")
        raise typer.Exit(code=1) from None


@app.command("backup-create")
def backup_create(
    include_database_dump: Annotated[
        bool,
        typer.Option(
            "--database-dump/--no-database-dump",
            "--postgres/--no-postgres",
            help="Deprecated compatibility flag; active backups ignore database dumps",
        ),
    ] = False,
    include_graph: Annotated[
        bool,
        typer.Option("--graph/--no-graph", help="Include graph export"),
    ] = True,
    wait: Annotated[
        bool,
        typer.Option("--wait", "-w", help="Wait for backup to complete"),
    ] = False,
) -> None:
    """Create a backup via the API (async job).

    Triggers a backup job on the server that creates a compressed archive
    containing Surreal runtime snapshots and graph data export.

    Use --wait to block until the backup completes.

    Example:
        sibyld db backup-create              # Queue backup job
        sibyld db backup-create --wait       # Wait for completion
        sibyld db backup-create --no-graph   # Auth/content snapshots only
    """
    import time

    info("Triggering backup job via API...")

    result = _api_request(
        "POST",
        "/backups",
        json_data={
            "include_database_dump": include_database_dump,
            "include_graph": include_graph,
        },
    )

    if not isinstance(result, dict):
        error("Unexpected response from API")
        raise typer.Exit(code=1)

    job_id = result.get("job_id", "unknown")
    success(f"Backup job queued: {job_id}")

    if wait:
        info("Waiting for backup to complete...")

        # Poll for completion
        while True:
            status_result = _api_request("GET", f"/backups/jobs/{job_id}")
            if not isinstance(status_result, dict):
                break

            status = status_result.get("status", "unknown")

            if status == "complete":
                job_result = status_result.get("result", {})
                if job_result.get("success"):
                    archive_path = job_result.get("archive_path", "unknown")
                    size_kb = job_result.get("archive_size_bytes", 0) / 1024
                    duration = job_result.get("duration_seconds", 0)
                    entities = job_result.get("entity_count", 0)
                    relationships = job_result.get("relationship_count", 0)

                    console.print()
                    success("Backup complete!")
                    info(f"  Archive: {archive_path}")
                    info(f"  Size: {size_kb:.1f} KB")
                    info(f"  Entities: {entities}, Relationships: {relationships}")
                    info(f"  Duration: {duration:.2f}s")
                else:
                    error(f"Backup failed: {job_result.get('error', 'unknown')}")
                break

            if status == "not_found":
                error("Job not found (may have been cleaned up)")
                break

            console.print(".", end="", style="dim")
            time.sleep(2)

        console.print()  # Newline after dots


@app.command("backup-list")
def backup_list(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
) -> None:
    """List all available backup archives.

    Shows backups sorted by creation time (newest first).

    Example:
        sibyld db backup-list
        sibyld db backup-list --json
    """
    result = _api_request("GET", "/backups")

    if not isinstance(result, dict):
        error("Unexpected response from API")
        raise typer.Exit(code=1)

    backups = result.get("backups", [])
    backup_dir = result.get("backup_dir", "unknown")

    if json_output:
        import json as json_module

        console.print(json_module.dumps(result, indent=2))
        return

    if not backups:
        info(f"No backups found in {backup_dir}")
        return

    console.print(f"\n[{NEON_CYAN}]Available Backups[/{NEON_CYAN}] ({backup_dir})\n")

    for b in backups:
        backup_id = b.get("backup_id", "unknown")
        size_kb = b.get("size_bytes", 0) / 1024
        created = b.get("created_at", "unknown")
        metadata = b.get("metadata", {})

        entities = metadata.get("graph_entities", "?") if metadata else "?"
        relationships = metadata.get("graph_relationships", "?") if metadata else "?"

        console.print(f"  [{ELECTRIC_PURPLE}]{backup_id}[/{ELECTRIC_PURPLE}]")
        console.print(f"    Created: {created}")
        console.print(f"    Size: {size_kb:.1f} KB")
        console.print(f"    Graph: {entities} entities, {relationships} relationships")
        console.print()


@app.command("backup-download")
def backup_download(
    backup_id: Annotated[str, typer.Argument(help="Backup ID to download")],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Output file path")] = None,
) -> None:
    """Download a backup archive.

    Example:
        sibyld db backup-download backup_20260110_153045
        sibyld db backup-download backup_20260110_153045 -o /tmp/backup.tar.gz
    """
    info(f"Downloading backup: {backup_id}...")

    # First get backup details for filename
    details = _api_request("GET", f"/backups/{backup_id}")
    if not isinstance(details, dict):
        error("Backup not found")
        raise typer.Exit(code=1)

    filename = details.get("filename", f"sibyl_{backup_id}.tar.gz")

    # Download the archive
    content = _api_request("GET", f"/backups/{backup_id}/download", stream=True)
    if not isinstance(content, bytes):
        error("Failed to download backup")
        raise typer.Exit(code=1)

    # Save to file
    output_path = output or Path(filename)
    output_path.write_bytes(content)

    size_kb = len(content) / 1024
    success(f"Downloaded: {output_path} ({size_kb:.1f} KB)")


@app.command("backup-delete")
def backup_delete(
    backup_id: Annotated[str, typer.Argument(help="Backup ID to delete")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a backup archive.

    This action cannot be undone.

    Example:
        sibyld db backup-delete backup_20260110_153045
        sibyld db backup-delete backup_20260110_153045 -y
    """
    if not yes:
        warn(f"This will permanently delete backup: {backup_id}")
        if not typer.confirm("Continue?"):
            info("Cancelled")
            return

    info(f"Deleting backup: {backup_id}...")

    result = _api_request("DELETE", f"/backups/{backup_id}")

    if isinstance(result, dict) and result.get("deleted"):
        success(f"Backup deleted: {backup_id}")
    else:
        error("Failed to delete backup")
        raise typer.Exit(code=1)


@app.command("backup-cleanup")
def backup_cleanup(
    retention_days: Annotated[
        int | None,
        typer.Option("--retention", "-r", help="Override retention period (days)"),
    ] = None,
) -> None:
    """Trigger backup cleanup job.

    Removes backup archives older than the retention period.

    Example:
        sibyld db backup-cleanup                # Use default retention
        sibyld db backup-cleanup --retention 7  # Keep only 7 days
    """
    info("Triggering backup cleanup job...")

    json_data = {}
    if retention_days is not None:
        json_data["retention_days"] = retention_days

    result = _api_request("POST", "/backups/cleanup", json_data=json_data)

    if isinstance(result, dict):
        job_id = result.get("job_id", "unknown")
        success(f"Cleanup job queued: {job_id}")
    else:
        error("Failed to queue cleanup job")
        raise typer.Exit(code=1)


@app.command("backup-settings")
def backup_settings() -> None:
    """Show backup configuration settings.

    Example:
        sibyld db backup-settings
    """
    result = _api_request("GET", "/backups/settings")

    if not isinstance(result, dict):
        error("Failed to get backup settings")
        raise typer.Exit(code=1)

    console.print(f"\n[{NEON_CYAN}]Backup Settings[/{NEON_CYAN}]\n")
    console.print(f"  Enabled: {result.get('backup_enabled', False)}")
    console.print(f"  Schedule: {result.get('backup_schedule', 'unknown')}")
    console.print(f"  Directory: {result.get('backup_dir', 'unknown')}")
    console.print(f"  Retention: {result.get('retention_days', 'unknown')} days")
    console.print()
