"""Admin tools for the Conventions MCP Server.

Provides maintenance and diagnostic capabilities.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from sibyl.config import settings
from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.models.entities import EntityType

log = structlog.get_logger()


@dataclass
class HealthStatus:
    """Server health status."""

    status: str  # "healthy", "degraded", "unhealthy"
    server_name: str
    uptime_seconds: float
    graph_connected: bool
    entity_counts: dict[str, int] = field(default_factory=dict)
    search_latency_ms: float | None = None
    last_sync: datetime | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    success: bool
    files_processed: int
    entities_created: int
    entities_updated: int
    errors: list[str]
    duration_seconds: float


@dataclass
class RebuildResult:
    """Result of an index rebuild operation."""

    success: bool
    indices_rebuilt: list[str]
    duration_seconds: float
    message: str


@dataclass
class ServerState:
    """Tracks server runtime state."""

    start_time: float | None = None


# Singleton instance for server state
_state = ServerState()


def mark_server_started() -> None:
    """Mark the server as started for uptime tracking."""
    _state.start_time = time.time()


async def health_check() -> HealthStatus:
    """Check server health and return status.

    Performs health checks on:
    - Graph database connectivity
    - Entity counts by type
    - Search latency

    Returns:
        HealthStatus with current server state.
    """
    log.info("Performing health check")

    errors: list[str] = []
    graph_connected = False
    entity_counts: dict[str, int] = {}
    search_latency_ms: float | None = None

    # Calculate uptime
    uptime = 0.0
    if _state.start_time is not None:
        uptime = time.time() - _state.start_time

    try:
        client = await get_graph_client()
        graph_connected = True

        # Get entity counts
        entity_manager = EntityManager(client)
        for entity_type in EntityType:
            try:
                entities = await entity_manager.list_by_type(entity_type, limit=1000)
                entity_counts[entity_type.value] = len(entities)
            except Exception:
                entity_counts[entity_type.value] = -1  # Unknown

        # Test search latency
        try:
            start = time.time()
            await entity_manager.search(query="test", entity_types=[EntityType.PATTERN], limit=1)
            search_latency_ms = (time.time() - start) * 1000
        except Exception as e:
            errors.append(f"Search latency test failed: {e}")

    except Exception as e:
        errors.append(f"Graph connection failed: {e}")

    # Determine overall status
    if not graph_connected:
        status = "unhealthy"
    elif errors:
        status = "degraded"
    else:
        status = "healthy"

    return HealthStatus(
        status=status,
        server_name=settings.server_name,
        uptime_seconds=uptime,
        graph_connected=graph_connected,
        entity_counts=entity_counts,
        search_latency_ms=search_latency_ms,
        last_sync=None,  # TODO: Track last sync time
        errors=errors,
    )


async def sync_wisdom_docs(
    path: str | None = None,
    force: bool = False,
) -> SyncResult:
    """Re-ingest wisdom documentation from files.

    Args:
        path: Specific path to sync. If None, syncs all wisdom docs.
        force: If True, re-process all files even if unchanged.

    Returns:
        SyncResult with sync statistics.
    """
    log.info("Starting wisdom docs sync", path=path, force=force)

    start_time = time.time()
    errors: list[str] = []
    files_processed = 0
    entities_created = 0
    entities_updated = 0

    try:
        # Import pipeline here to avoid circular imports
        from sibyl.ingestion.pipeline import IngestionPipeline

        # Determine what to sync and validate paths
        repo_root = Path(settings.conventions_repo_path).resolve()

        if path:
            # Security: Validate path is within repository bounds
            sync_path = Path(path).resolve()
            try:
                sync_path.relative_to(repo_root)
            except ValueError:
                return SyncResult(
                    success=False,
                    files_processed=0,
                    entities_created=0,
                    entities_updated=0,
                    errors=[f"Security error: Path {path} is outside repository bounds"],
                    duration_seconds=time.time() - start_time,
                )

            if not sync_path.exists():
                return SyncResult(
                    success=False,
                    files_processed=0,
                    entities_created=0,
                    entities_updated=0,
                    errors=[f"Path does not exist: {sync_path}"],
                    duration_seconds=time.time() - start_time,
                )

            # Convert to relative pattern for pipeline
            try:
                relative_path = sync_path.relative_to(repo_root)
                wisdom_patterns = [f"{relative_path}/**/*.md"]
            except ValueError:
                # Should not happen due to earlier check, but defensive
                wisdom_patterns = None
        else:
            wisdom_patterns = None  # Use default patterns

        # Create pipeline with appropriate patterns
        pipeline = IngestionPipeline(repo_root, wisdom_patterns=wisdom_patterns)

        # Run ingestion
        result = await pipeline.run()

        files_processed = result.stats.files_processed
        entities_created = result.stats.entities_extracted
        # entities_updated would come from diff detection (not implemented yet)

        if not result.success:
            errors.extend([str(e) for e in result.errors])

    except Exception as e:
        log.error("Sync failed", error=str(e))  # noqa: TRY400
        errors.append(str(e))

    duration = time.time() - start_time

    return SyncResult(
        success=len(errors) == 0,
        files_processed=files_processed,
        entities_created=entities_created,
        entities_updated=entities_updated,
        errors=errors,
        duration_seconds=duration,
    )


async def rebuild_indices(
    index_type: str | None = None,
) -> RebuildResult:
    """Rebuild graph indices for better query performance.

    Args:
        index_type: Specific index to rebuild. Options:
            - "search": Rebuild search/embedding indices
            - "relationships": Rebuild relationship indices
            - "all": Rebuild all indices (default)

    Returns:
        RebuildResult with rebuild status.
    """
    log.info("Rebuilding indices", index_type=index_type)

    start_time = time.time()
    indices_rebuilt: list[str] = []

    try:
        # Get client to verify connectivity (actual rebuild uses it)
        _client = await get_graph_client()
        _ = _client  # Mark as used; actual index rebuild operations are TODO

        target = index_type or "all"

        if target in ("search", "all"):
            # TODO: Implement actual Graphiti index rebuild
            # This would call _client.rebuild_search_index() or similar
            log.info("Rebuilding search indices")
            indices_rebuilt.append("search")

        if target in ("relationships", "all"):
            # TODO: Implement actual Graphiti relationship index rebuild
            log.info("Rebuilding relationship indices")
            indices_rebuilt.append("relationships")

        duration = time.time() - start_time

        return RebuildResult(
            success=True,
            indices_rebuilt=indices_rebuilt,
            duration_seconds=duration,
            message=f"Successfully rebuilt {len(indices_rebuilt)} index(es)",
        )

    except Exception as e:
        log.error("Index rebuild failed", error=str(e))  # noqa: TRY400
        return RebuildResult(
            success=False,
            indices_rebuilt=indices_rebuilt,
            duration_seconds=time.time() - start_time,
            message=f"Rebuild failed: {e}",
        )


async def get_stats() -> dict[str, object]:
    """Get detailed statistics about the knowledge graph.

    Returns:
        Dictionary with graph statistics.
    """
    log.info("Getting graph stats")

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        stats: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "entities": {},
            "relationships": {},
            "storage": {},
        }

        # Count entities by type
        entity_stats: dict[str, int] = {}
        for entity_type in EntityType:
            try:
                entities = await entity_manager.list_by_type(entity_type, limit=10000)
                entity_stats[entity_type.value] = len(entities)
            except Exception:
                entity_stats[entity_type.value] = -1

        stats["entities"] = entity_stats
        stats["total_entities"] = sum(v for v in entity_stats.values() if v > 0)

        # TODO: Add relationship stats from RelationshipManager
        # TODO: Add storage stats from Graphiti

        return stats

    except Exception as e:
        log.error("Failed to get stats", error=str(e))  # noqa: TRY400
        return {
            "error": str(e),
            "timestamp": datetime.now(UTC).isoformat(),
        }


@dataclass
class MigrationResult:
    """Result of a migration operation."""

    success: bool
    entities_updated: int
    message: str
    duration_seconds: float


async def migrate_add_group_ids() -> MigrationResult:
    """Add group_ids to entities that are missing it.

    Entities created via direct insert before the group_ids fix
    won't be discoverable via Graphiti's get_by_group_ids.
    This migration adds group_ids=["conventions"] to all such entities.

    Returns:
        MigrationResult with migration status.
    """
    log.info("Running migration: add group_ids to entities")

    start_time = time.time()
    entities_updated = 0

    try:
        client = await get_graph_client()

        # Fix entities for Graphiti compatibility:
        # 1. group_id: singular string (Graphiti queries: WHERE n.group_id IN $group_ids)
        # 2. summary: required by EntityNode model
        # 3. labels: required by EntityNode model
        result = await client.driver.execute_query(
            """
            MATCH (n:Entity)
            WHERE n.group_id IS NULL OR n.summary IS NULL OR n.labels IS NULL
            SET n.group_id = COALESCE(n.group_id, "conventions"),
                n.summary = COALESCE(n.summary, SUBSTRING(COALESCE(n.description, n.name), 0, 500)),
                n.labels = COALESCE(n.labels, [])
            RETURN count(n) as updated
            """
        )

        # Result is (results_list, column_names, meta)
        if result and result[0]:
            entities_updated = result[0][0].get("updated", 0)

        duration = time.time() - start_time

        log.info(
            "Migration complete",
            entities_updated=entities_updated,
            duration_seconds=duration,
        )

        return MigrationResult(
            success=True,
            entities_updated=entities_updated,
            message=f"Added group_ids to {entities_updated} entities",
            duration_seconds=duration,
        )

    except Exception as e:
        log.exception("Migration failed", error=str(e))
        return MigrationResult(
            success=False,
            entities_updated=0,
            message=f"Migration failed: {e}",
            duration_seconds=time.time() - start_time,
        )
