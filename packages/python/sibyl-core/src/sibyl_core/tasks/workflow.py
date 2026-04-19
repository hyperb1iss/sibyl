"""Task workflow engine for status transitions and automations."""

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from sibyl_core.errors import InvalidTransitionError
from sibyl_core.models.entities import EntityType, Relationship, RelationshipType
from sibyl_core.models.tasks import EpicStatus, Task, TaskStatus
from sibyl_core.tasks.distillation import build_learning_episode, build_learning_procedure

if TYPE_CHECKING:
    from sibyl_core.graph.client import GraphClient
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager

log = structlog.get_logger()


# =============================================================================
# State Machine Definition
# =============================================================================

# All statuses except ARCHIVED (which is terminal)
ALL_STATUSES = {s for s in TaskStatus if s != TaskStatus.ARCHIVED}


def is_valid_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    """Check if a status transition is valid.

    Allows any transition except out of ARCHIVED (terminal state).

    Args:
        from_status: Current status
        to_status: Target status

    Returns:
        True if transition is allowed
    """
    # No-op is valid, ARCHIVED is terminal, everything else is allowed
    return from_status == to_status or from_status != TaskStatus.ARCHIVED


def get_allowed_transitions(status: TaskStatus) -> set[TaskStatus]:
    """Get allowed transitions from a given status.

    Args:
        status: Current status

    Returns:
        Set of valid target statuses
    """
    if status == TaskStatus.ARCHIVED:
        return set()  # Terminal state
    return ALL_STATUSES | {TaskStatus.ARCHIVED}


class TaskWorkflowEngine:
    """Handles task status transitions and automations.

    Allows flexible status transitions - any status can transition to any
    other status, with one constraint:

    - ARCHIVED is a terminal state (no transitions out)

    This enables ad-hoc workflows without enforcing a rigid state machine.
    """

    def __init__(
        self,
        entity_manager: "EntityManager",
        relationship_manager: "RelationshipManager",
        graph_client: "GraphClient",
        organization_id: str,
    ) -> None:
        """Initialize workflow engine with graph managers."""
        self._entity_manager = entity_manager
        self._relationship_manager = relationship_manager
        self._graph_client = graph_client
        self._organization_id = organization_id

    def _uses_surreal_runtime(self) -> bool:
        if getattr(self._graph_client, "_store", None) == "surreal":
            return True

        try:
            from sibyl_core.backends.surreal import SurrealDriver
        except ImportError:
            return False

        driver = getattr(self._graph_client, "driver", None)
        if driver is None:
            graphiti_client = getattr(self._graph_client, "client", None)
            driver = getattr(graphiti_client, "driver", None)
        return isinstance(driver, SurrealDriver)

    def _validate_transition(
        self,
        current_status: TaskStatus,
        target_status: TaskStatus,
    ) -> None:
        """Validate that a status transition is allowed.

        Args:
            current_status: Current task status
            target_status: Desired status

        Raises:
            InvalidTransitionError: If transition is not allowed
        """
        if not is_valid_transition(current_status, target_status):
            allowed = get_allowed_transitions(current_status)
            raise InvalidTransitionError(
                from_status=current_status.value,
                to_status=target_status.value,
                allowed=[s.value for s in allowed],
            )

    async def transition_task(
        self,
        task_id: str,
        target_status: TaskStatus,
        updates: dict | None = None,
    ) -> Task:
        """Transition a task to a new status with validation.

        This is the core transition method that all other workflow
        methods build upon. It validates the transition against the
        state machine before applying it.

        Args:
            task_id: Task UUID
            target_status: Desired status
            updates: Additional field updates to apply

        Returns:
            Updated task

        Raises:
            InvalidTransitionError: If transition is not allowed
            EntityNotFoundError: If task doesn't exist
        """
        log.info(
            "Transitioning task",
            task_id=task_id,
            target_status=target_status.value,
        )

        # Get current task
        entity = await self._entity_manager.get(task_id)
        task = self._entity_to_task(entity)

        # Validate transition
        self._validate_transition(task.status, target_status)

        # Build updates
        all_updates = updates or {}
        if target_status != task.status:
            all_updates["status"] = target_status

        # Apply updates
        if all_updates:
            updated_entity = await self._entity_manager.update(task_id, all_updates)
            task = self._entity_to_task(updated_entity)

        log.info(
            "Task transitioned",
            task_id=task_id,
            from_status=entity.metadata.get("status"),
            to_status=target_status.value,
        )
        return task

    async def start_task(self, task_id: str, assignee: str) -> Task:
        """Transition task to 'doing' status.

        Args:
            task_id: Task UUID
            assignee: User starting the task

        Returns:
            Updated task

        Raises:
            InvalidTransitionError: If task is not in TODO status
        """
        log.info("Starting task", task_id=task_id, assignee=assignee)

        # Get current task
        entity = await self._entity_manager.get(task_id)
        task = self._entity_to_task(entity)

        # Validate transition
        self._validate_transition(task.status, TaskStatus.DOING)

        # Build updates
        updates: dict = {
            "status": TaskStatus.DOING,
            "started_at": datetime.now(UTC),
        }

        # Add assignee if not already assigned
        if assignee not in task.assignees:
            updates["assignees"] = [*task.assignees, assignee]

        # Auto-suggest branch name if not set
        if not task.branch_name:
            branch_name = self._generate_branch_name(task)
            updates["branch_name"] = branch_name
            log.info("Generated branch name", task_id=task_id, branch=branch_name)

        # Update task
        updated_entity = await self._entity_manager.update(task_id, updates)
        updated_task = self._entity_to_task(updated_entity)

        # Update project activity timestamp
        if task.project_id:
            await self.update_project_activity(task.project_id)

        # Auto-start epic if still in planning
        epic_started = await self._maybe_start_epic(updated_task)
        if epic_started:
            log.info("Epic auto-started", epic_id=task.epic_id, task_id=task_id)

        log.info("Task started successfully", task_id=task_id, branch=updated_task.branch_name)
        return updated_task

    async def submit_for_review(
        self, task_id: str, commit_shas: list[str], pr_url: str | None = None
    ) -> Task:
        """Move task to review status.

        Args:
            task_id: Task UUID
            commit_shas: Git commit SHAs implementing this task
            pr_url: Pull request URL

        Returns:
            Updated task

        Raises:
            InvalidTransitionError: If task is not in DOING status
        """
        log.info("Submitting task for review", task_id=task_id, pr_url=pr_url)

        # Get current task and validate transition
        entity = await self._entity_manager.get(task_id)
        task = self._entity_to_task(entity)
        self._validate_transition(task.status, TaskStatus.REVIEW)

        updates: dict = {
            "status": TaskStatus.REVIEW,
            "commit_shas": commit_shas,
            "reviewed_at": datetime.now(UTC),
        }

        if pr_url:
            updates["pr_url"] = pr_url

        updated_entity = await self._entity_manager.update(task_id, updates)
        updated_task = self._entity_to_task(updated_entity)

        # Update project activity timestamp
        if task.project_id:
            await self.update_project_activity(task.project_id)

        # Auto-start epic if still in planning
        epic_started = await self._maybe_start_epic(updated_task)
        if epic_started:
            log.info("Epic auto-started", epic_id=task.epic_id, task_id=task_id)

        log.info("Task submitted for review", task_id=task_id, commits=len(commit_shas))
        return updated_task

    async def complete_task(
        self,
        task_id: str,
        actual_hours: float | None = None,
        learnings: str = "",
        *,
        create_episode: bool = True,
    ) -> Task:
        """Mark task as done and capture learnings.

        Args:
            task_id: Task UUID
            actual_hours: Actual time spent on task
            learnings: What was learned completing this task
            create_episode: Whether to create learning episode synchronously.
                Set to False when using async job queue for episode creation.

        Returns:
            Updated task

        Raises:
            InvalidTransitionError: If task is not in DOING or REVIEW status
        """
        log.info("Completing task", task_id=task_id)

        # Get current task and validate transition
        entity = await self._entity_manager.get(task_id)
        task = self._entity_to_task(entity)
        self._validate_transition(task.status, TaskStatus.DONE)

        # Build updates
        updates: dict = {
            "status": TaskStatus.DONE,
            "completed_at": datetime.now(UTC),
        }

        if actual_hours is not None:
            updates["actual_hours"] = actual_hours

        if learnings:
            updates["learnings"] = learnings

        # Update task
        updated_entity = await self._entity_manager.update(task_id, updates)
        updated_task = self._entity_to_task(updated_entity)

        # Create learning artifacts from completed task if learnings provided.
        # Skip if create_episode=False (caller will handle async jobs instead).
        if learnings and create_episode:
            await self._create_learning_episode(updated_task)
            await self._create_learning_procedure(updated_task)

        # Update project progress
        if task.project_id:
            await self._update_project_progress(task.project_id)

        # Auto-complete epic if all tasks are done
        epic_completed = await self._maybe_complete_epic(updated_task)
        if epic_completed:
            log.info("Epic auto-completed", epic_id=task.epic_id, task_id=task_id)

        log.info("Task completed successfully", task_id=task_id)
        return updated_task

    async def block_task(self, task_id: str, blocker_description: str) -> Task:
        """Mark task as blocked.

        Args:
            task_id: Task UUID
            blocker_description: Description of the blocker

        Returns:
            Updated task

        Raises:
            InvalidTransitionError: If task is not in DOING status
        """
        log.info("Blocking task", task_id=task_id)

        # Get current task and validate transition
        entity = await self._entity_manager.get(task_id)
        task = self._entity_to_task(entity)
        self._validate_transition(task.status, TaskStatus.BLOCKED)

        # Add blocker to list
        blockers = [*task.blockers_encountered, blocker_description]

        updates: dict = {
            "status": TaskStatus.BLOCKED,
            "blockers_encountered": blockers,
        }

        updated_entity = await self._entity_manager.update(task_id, updates)
        updated_task = self._entity_to_task(updated_entity)

        # Update project activity timestamp
        if task.project_id:
            await self.update_project_activity(task.project_id)

        # Auto-start epic if still in planning (blocked = still working on it)
        epic_started = await self._maybe_start_epic(updated_task)
        if epic_started:
            log.info("Epic auto-started", epic_id=task.epic_id, task_id=task_id)

        log.info("Task blocked", task_id=task_id, blocker=blocker_description)
        return updated_task

    async def unblock_task(self, task_id: str) -> Task:
        """Unblock a task and return to doing status.

        Args:
            task_id: Task UUID

        Returns:
            Updated task

        Raises:
            InvalidTransitionError: If task is not in BLOCKED status
        """
        log.info("Unblocking task", task_id=task_id)

        # Get current task and validate transition
        entity = await self._entity_manager.get(task_id)
        task = self._entity_to_task(entity)
        self._validate_transition(task.status, TaskStatus.DOING)

        updates: dict = {
            "status": TaskStatus.DOING,
        }

        updated_entity = await self._entity_manager.update(task_id, updates)
        updated_task = self._entity_to_task(updated_entity)

        # Update project activity timestamp
        if task.project_id:
            await self.update_project_activity(task.project_id)

        log.info("Task unblocked", task_id=task_id)
        return updated_task

    async def archive_task(self, task_id: str, reason: str = "") -> Task:
        """Archive a task without completing it.

        Args:
            task_id: Task UUID
            reason: Reason for archiving

        Returns:
            Updated task

        Raises:
            InvalidTransitionError: If task is already ARCHIVED
        """
        log.info("Archiving task", task_id=task_id, reason=reason)

        # Get current task and validate transition
        entity = await self._entity_manager.get(task_id)
        task = self._entity_to_task(entity)
        self._validate_transition(task.status, TaskStatus.ARCHIVED)

        updates: dict = {
            "status": TaskStatus.ARCHIVED,
        }
        if reason:
            updates["metadata"] = {**(task.metadata or {}), "archive_reason": reason}

        updated_entity = await self._entity_manager.update(task_id, updates)
        updated_task = self._entity_to_task(updated_entity)

        # Update project progress
        if task.project_id:
            await self._update_project_progress(task.project_id)

        # Auto-complete epic if all tasks are done/archived
        epic_completed = await self._maybe_complete_epic(updated_task)
        if epic_completed:
            log.info("Epic auto-completed", epic_id=task.epic_id, task_id=task_id)

        log.info("Task archived", task_id=task_id)
        return updated_task

    async def _create_learning_episode(self, task: Task) -> str:
        """Convert completed task into a knowledge episode.

        Args:
            task: Completed task with learnings

        Returns:
            Episode UUID
        """
        log.info("Creating learning episode from task", task_id=task.id)

        episode = build_learning_episode(task)

        # Use Graphiti create for proper relationship discovery from learnings
        episode_id = await self._entity_manager.create(episode)

        # Link episode back to task
        await self._relationship_manager.create(
            Relationship(
                id=f"rel_episode_{task.id}",
                source_id=episode_id,
                target_id=task.id,
                relationship_type=RelationshipType.DERIVED_FROM,
            )
        )

        await self._inherit_task_knowledge(episode_id, task.id)

        log.info("Learning episode created", episode_id=episode_id, task_id=task.id)
        return episode_id

    async def _create_learning_procedure(self, task: Task) -> str | None:
        """Convert completed task learnings into a reusable procedure."""
        log.info("Creating learning procedure from task", task_id=task.id)

        note_contents = await self._get_task_note_contents(task.id)
        procedure = build_learning_procedure(task, note_contents)
        if procedure is None:
            log.info("Learning procedure skipped", task_id=task.id)
            return None

        procedure_id = await self._entity_manager.create_direct(procedure)

        await self._relationship_manager.create(
            Relationship(
                id=f"rel_task_{task.id}_procedure",
                source_id=task.id,
                target_id=procedure_id,
                relationship_type=RelationshipType.USES_PROCEDURE,
            )
        )
        await self._relationship_manager.create(
            Relationship(
                id=f"rel_procedure_{task.id}",
                source_id=procedure_id,
                target_id=task.id,
                relationship_type=RelationshipType.DERIVED_FROM,
            )
        )

        await self._inherit_task_knowledge(procedure_id, task.id)

        log.info("Learning procedure created", procedure_id=procedure_id, task_id=task.id)
        return procedure_id

    async def _get_task_note_contents(self, task_id: str) -> list[str]:
        get_notes = getattr(self._entity_manager, "get_notes_for_task", None)
        if not callable(get_notes):
            return []

        try:
            notes = await get_notes(task_id, limit=20)
        except Exception as exc:
            log.debug(
                "Failed to load task notes for distillation",
                task_id=task_id,
                error=str(exc),
            )
            return []

        return [note.content for note in notes if getattr(note, "content", "").strip()]

    async def _inherit_task_knowledge(self, source_id: str, task_id: str) -> None:
        task_relationships = await self._relationship_manager.get_for_entity(
            task_id,
            relationship_types=[
                RelationshipType.REQUIRES,
                RelationshipType.REFERENCES,
                RelationshipType.PART_OF,
            ],
        )

        for rel in task_relationships:
            await self._relationship_manager.create(
                Relationship(
                    id=f"rel_inherit_{source_id}_{rel.target_id}",
                    source_id=source_id,
                    target_id=rel.target_id,
                    relationship_type=RelationshipType.REFERENCES,
                    metadata={"inherited_from_task": task_id},
                )
            )

    async def update_project_activity(self, project_id: str) -> None:
        """Update project's last_activity_at timestamp.

        Called when any child entity (task/epic) changes.

        Args:
            project_id: Project UUID
        """
        now = datetime.now(UTC)
        await self._entity_manager.update(
            project_id,
            {"last_activity_at": now.isoformat()},
        )
        log.debug(
            "Project activity updated", project_id=project_id, last_activity_at=now.isoformat()
        )

    async def _update_project_progress(self, project_id: str) -> None:
        """Update project progress statistics.

        Args:
            project_id: Project UUID
        """
        log.debug("Updating project progress", project_id=project_id)

        total = 0
        done = 0
        doing = 0

        if self._uses_surreal_runtime():
            tasks = await self._entity_manager.list_by_type(
                EntityType.TASK,
                project_id=project_id,
                limit=10_000,
                include_archived=True,
            )
            metadata_rows = [task.metadata or {} for task in tasks]
        else:
            # Query project tasks using the current graph shape:
            # task nodes BELONGS_TO a project, and status typically lives in metadata.
            query = """
            MATCH (t)-[:BELONGS_TO]->(p)
            WHERE t.entity_type = 'task'
              AND p.entity_type = 'project'
              AND t.group_id = $group_id
              AND p.uuid = $project_id
            RETURN t.metadata AS metadata
            """

            rows = await self._graph_client.execute_read_org(
                query,
                self._organization_id,
                group_id=self._organization_id,
                project_id=project_id,
            )
            metadata_rows = []
            for record in rows:
                metadata = record.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {}
                metadata_rows.append(metadata)

        for metadata in metadata_rows:
            status = (metadata.get("status") if isinstance(metadata, dict) else None) or "todo"
            total += 1
            if status == "done":
                done += 1
            elif status == "doing":
                doing += 1

        # Update project entity with progress and activity timestamp
        now = datetime.now(UTC)
        await self._entity_manager.update(
            project_id,
            {
                "total_tasks": total,
                "completed_tasks": done,
                "in_progress_tasks": doing,
                "last_activity_at": now.isoformat(),
            },
        )

        log.debug(
            "Project progress updated",
            project_id=project_id,
            total=total,
            done=done,
            doing=doing,
            last_activity_at=now.isoformat(),
        )

    async def _maybe_start_epic(self, task: Task) -> bool:
        """Auto-start epic if a task moves to a forward-progress state.

        Epics should transition from 'planning' to 'in_progress' when any
        task starts active work (doing, review, blocked).

        Args:
            task: The task that just transitioned to a forward-progress state

        Returns:
            True if epic was auto-started, False otherwise
        """
        epic_id = task.epic_id
        if not epic_id:
            return False

        # Only trigger on forward-progress states
        forward_progress_states = {TaskStatus.DOING, TaskStatus.REVIEW, TaskStatus.BLOCKED}
        if task.status not in forward_progress_states:
            return False

        log.debug("Checking epic auto-start", epic_id=epic_id, task_id=task.id)

        # Get epic's current status
        epic = await self._entity_manager.get(epic_id)
        current_status = epic.metadata.get("status", "planning")

        # Only auto-start if epic is in planning
        if current_status != "planning":
            return False

        log.info(
            "Auto-starting epic - task in progress",
            epic_id=epic_id,
            task_id=task.id,
            task_status=task.status.value,
        )

        await self._entity_manager.update(
            epic_id,
            {
                "status": EpicStatus.IN_PROGRESS,
                "started_at": datetime.now(UTC),
            },
        )
        return True

    async def _maybe_complete_epic(self, task: Task) -> bool:
        """Auto-complete epic if all its tasks are done.

        Epics are organizational containers - they auto-complete when all
        their tasks reach terminal states (done or archived).

        Args:
            task: The task that was just completed/archived

        Returns:
            True if epic was auto-completed, False otherwise
        """
        epic_id = task.epic_id
        if not epic_id:
            return False

        log.debug("Checking epic auto-completion", epic_id=epic_id, task_id=task.id)

        if self._uses_surreal_runtime():
            epic = await self._entity_manager.get(epic_id)
            current_status = str((epic.metadata or {}).get("status") or "planning")
            tasks = await self._entity_manager.list_by_type(
                EntityType.TASK,
                epic_id=epic_id,
                limit=10_000,
                include_archived=True,
            )
            total = len(tasks)
            terminal = 0
            for epic_task in tasks:
                task_status = str((epic_task.metadata or {}).get("status") or "").lower()
                if task_status in {"done", "archived"}:
                    terminal += 1
        else:
            # Query all tasks in this epic and their statuses
            query = """
            MATCH (epic {uuid: $epic_id})<-[:BELONGS_TO]-(t)
            WHERE t.entity_type = 'task'
            WITH epic,
                 count(t) as total,
                 count(CASE WHEN t.status IN ['done', 'archived'] THEN 1 END) as terminal
            RETURN epic.status as epic_status, total, terminal
            """

            rows = await self._graph_client.execute_read_org(
                query, self._organization_id, epic_id=epic_id
            )

            if not rows:
                log.warning("Epic not found for auto-completion", epic_id=epic_id)
                return False

            record = rows[0]
            total = record.get("total", 0)
            terminal = record.get("terminal", 0)
            current_status = record.get("epic_status", "planning")

        # Already completed or no tasks
        if current_status in ["completed", "archived"] or total == 0:
            return False

        # All tasks in terminal state - auto-complete the epic
        if total == terminal:
            log.info(
                "Auto-completing epic - all tasks done",
                epic_id=epic_id,
                total_tasks=total,
            )

            await self._entity_manager.update(
                epic_id,
                {
                    "status": EpicStatus.COMPLETED,
                    "completed_date": datetime.now(UTC),
                    "total_tasks": total,
                    "completed_tasks": terminal,
                },
            )
            return True

        # Update progress stats even if not complete
        await self._entity_manager.update(
            epic_id,
            {
                "total_tasks": total,
                "completed_tasks": terminal,
            },
        )
        return False

    def _generate_branch_name(self, task: Task) -> str:
        """Generate conventional branch name for task.

        Args:
            task: Task to generate branch for

        Returns:
            Branch name following convention
        """
        # Use first 8 chars of task ID
        task_num = task.id[:8]

        # Slugify title
        slug = task.title.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = slug.strip("-")[:50]

        # Determine prefix based on feature/complexity
        if task.complexity == "epic":
            prefix = "epic"
        elif task.feature:
            prefix = "feature"
        else:
            prefix = "task"

        return f"{prefix}/{task_num}-{slug}"

    def _entity_to_task(self, entity) -> Task:
        """Convert Entity to Task model.

        Args:
            entity: Entity from entity manager

        Returns:
            Task instance
        """
        # If already a Task, return it directly
        if isinstance(entity, Task):
            return entity

        # Extract task-specific fields from metadata, excluding fields we pass explicitly
        metadata = entity.metadata or {}
        excluded_keys = {
            "id",
            "entity_type",
            "title",
            "description",
            "name",
            "content",
            "created_at",
            "updated_at",
        }
        task_fields = {k: v for k, v in metadata.items() if k not in excluded_keys}

        return Task(
            id=entity.id,
            entity_type=entity.entity_type,
            title=entity.name,
            description=entity.description,
            name=entity.name,
            content=entity.content,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            # Task-specific fields from metadata
            **task_fields,
        )
