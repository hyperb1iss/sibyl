"""MaterializationService for creating Sibyl entities from planning outputs.

Takes the planning session's spec draft and task drafts and materializes them
into the Sibyl knowledge graph as:
- Epic (for the feature/initiative)
- Tasks (linked to the epic)
- Document (spec as a reference doc)
- Episode (planning insights as learnings)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from sibyl.db import get_session
from sibyl.db.models import PlanningPhase
from sibyl.planning.service import PlanningSessionService
from sibyl_core.graph import EntityManager
from sibyl_core.graph.client import get_graph_client
from sibyl_core.models import (
    Document,
    Epic,
    EpicStatus,
    Episode,
    Task,
    TaskPriority,
    TaskStatus,
)

log = structlog.get_logger()


def _parse_priority(priority_str: str) -> TaskPriority:
    """Parse priority string to TaskPriority enum."""
    mapping = {
        "critical": TaskPriority.CRITICAL,
        "high": TaskPriority.HIGH,
        "medium": TaskPriority.MEDIUM,
        "low": TaskPriority.LOW,
        "someday": TaskPriority.SOMEDAY,
    }
    return mapping.get(priority_str.lower(), TaskPriority.MEDIUM)


async def materialize_planning_session(  # noqa: PLR0915
    session_id: UUID,
    org_id: UUID,
    *,
    project_id: UUID | None = None,
    epic_title: str | None = None,
    epic_priority: str = "medium",
) -> dict[str, Any]:
    """Materialize a planning session into Sibyl entities.

    Creates:
    1. Epic - for the overall feature/initiative
    2. Tasks - from task_drafts, linked to the epic
    3. Document - the spec_draft as a reference document
    4. Episode - the synthesis as a learning episode

    Args:
        session_id: Planning session UUID
        org_id: Organization UUID
        project_id: Optional project to assign entities to
        epic_title: Override title for the epic
        epic_priority: Priority for the epic

    Returns:
        Dict with created entity IDs
    """
    async with get_session() as db_session:
        service = PlanningSessionService(db_session)

        # Get session
        session = await service.get_session(session_id, org_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        if session.phase != PlanningPhase.ready:
            raise ValueError(f"Session not in ready phase: {session.phase}")

        # Use session's project if none provided
        if project_id is None:
            project_id = session.project_id

        # Connect to graph
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=str(org_id))

        result: dict[str, Any] = {
            "session_id": str(session_id),
            "epic_id": None,
            "task_ids": [],
            "document_id": None,
            "episode_id": None,
        }

        # 1. Create Epic
        epic_title_final = epic_title or session.title or f"Planning: {session.prompt[:50]}..."
        epic_id = f"epic_{uuid.uuid4().hex[:12]}"

        # Epic requires project_id - use session's project or raise
        if not project_id:
            raise ValueError("project_id is required to create an Epic")

        epic = Epic(
            id=epic_id,
            name=epic_title_final,
            title=epic_title_final,
            description=session.prompt,
            project_id=str(project_id),
            status=EpicStatus.PLANNING,
            priority=_parse_priority(epic_priority),
            total_tasks=len(session.task_drafts or []),
            completed_tasks=0,
        )

        await entity_manager.create_direct(epic)
        result["epic_id"] = epic.id

        log.info("Created epic", epic_id=epic.id, title=epic_title_final)

        # 2. Create Tasks from drafts
        task_drafts = session.task_drafts or []
        task_id_map: dict[int, str] = {}  # Map draft index to task ID

        for i, draft in enumerate(task_drafts):
            task_id = f"task_{uuid.uuid4().hex[:12]}"
            task_title = draft.get("title", f"Task {i + 1}")
            task = Task(
                id=task_id,
                name=task_title,
                title=task_title,
                description=draft.get("description", ""),
                project_id=str(project_id),
                epic_id=epic.id,
                status=TaskStatus.BACKLOG,
                priority=_parse_priority(draft.get("priority", "medium")),
                tags=draft.get("tags", []),
            )

            await entity_manager.create_direct(task)
            task_id_map[i] = task.id
            result["task_ids"].append(task.id)

            log.debug("Created task", task_id=task.id, title=task.title)

        # 3. Create task dependencies based on depends_on indices
        for i, draft in enumerate(task_drafts):
            depends_on = draft.get("depends_on", [])
            if depends_on and i in task_id_map:
                task_id = task_id_map[i]
                for dep_idx in depends_on:
                    if isinstance(dep_idx, int) and dep_idx in task_id_map:
                        dep_task_id = task_id_map[dep_idx]
                        await entity_manager.add_relationship(
                            task_id,
                            dep_task_id,
                            "DEPENDS_ON",
                        )

        log.info("Created tasks", count=len(task_id_map))

        # 4. Create Document from spec_draft
        if session.spec_draft:
            doc_id = f"doc_{uuid.uuid4().hex[:12]}"
            doc_title = f"Spec: {epic_title_final}"
            doc = Document(
                id=doc_id,
                name=doc_title,
                title=doc_title,
                content=session.spec_draft,
                # Use a sentinel source_id and URL for planning-generated docs
                source_id=f"planning_session:{session_id}",
                url=f"planning://sessions/{session_id}/spec",
                metadata={
                    "planning_session_id": str(session_id),
                    "epic_id": epic.id,
                    "project_id": str(project_id),
                    "source_type": "planning_studio",
                },
            )

            await entity_manager.create_direct(doc)
            result["document_id"] = doc.id

            # Link document to epic
            await entity_manager.add_relationship(
                doc.id,
                epic.id,
                "DOCUMENTS",
            )

            log.info("Created spec document", document_id=doc.id)

        # 5. Create Episode from synthesis
        if session.synthesis:
            episode_id = f"epsd_{uuid.uuid4().hex[:12]}"
            truncated_title = (
                epic_title_final[:40] + "..." if len(epic_title_final) > 40 else epic_title_final
            )
            episode = Episode(
                id=episode_id,
                name=f"Planning Insights: {truncated_title}",
                content=session.synthesis,
                episode_type="planning_synthesis",
                metadata={
                    "planning_session_id": str(session_id),
                    "epic_id": epic.id,
                    "project_id": str(project_id),
                    "source_description": "Multi-agent brainstorming synthesis",
                },
            )

            await entity_manager.create_direct(episode)
            result["episode_id"] = episode.id

            # Link episode to epic
            await entity_manager.add_relationship(
                episode.id,
                epic.id,
                "DISCOVERED_DURING",
            )

            log.info("Created synthesis episode", episode_id=episode.id)

        # 6. Update session with materialization info
        await service.update_session(
            session_id,
            org_id,
            phase=PlanningPhase.materialized,
        )

        # Update with entity references
        session.epic_id = epic.id
        session.task_ids = result["task_ids"]
        session.document_id = result.get("document_id")
        session.episode_id = result.get("episode_id")
        session.materialized_at = datetime.now(UTC).replace(tzinfo=None)

        await db_session.commit()

        # Publish event
        from sibyl.api.pubsub import publish_event

        await publish_event(
            "planning_session_materialized",
            {
                "session_id": str(session_id),
                "org_id": str(org_id),
                "epic_id": epic.id,
                "task_count": len(result["task_ids"]),
            },
        )

        log.info(
            "Planning session materialized",
            session_id=str(session_id),
            epic_id=epic.id,
            task_count=len(result["task_ids"]),
        )

        return result


async def run_materialization_job(
    session_id: UUID,
    org_id: UUID,
    project_id: UUID | None = None,
    epic_title: str | None = None,
    epic_priority: str = "medium",
) -> dict[str, Any]:
    """Job entry point for materialization.

    Designed to be called from the ARQ worker.

    Args:
        session_id: Planning session UUID
        org_id: Organization UUID
        project_id: Optional project to assign entities to
        epic_title: Override title for the epic
        epic_priority: Priority for the epic

    Returns:
        Materialization result dict
    """
    log.info("Starting materialization job", session_id=str(session_id))

    try:
        result = await materialize_planning_session(
            session_id,
            org_id,
            project_id=project_id,
            epic_title=epic_title,
            epic_priority=epic_priority,
        )

        log.info("Materialization job complete", session_id=str(session_id))

        return result

    except Exception as e:
        log.exception("Materialization job failed", session_id=str(session_id))
        raise RuntimeError(f"Materialization failed: {e}") from e
