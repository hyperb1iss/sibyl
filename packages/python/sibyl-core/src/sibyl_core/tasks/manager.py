"""Task manager for creating and querying tasks with knowledge integration."""

import asyncio
import uuid
from typing import TYPE_CHECKING

import structlog

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.tasks import (
    SimilarTaskInfo,
    Task,
    TaskEstimate,
    TaskKnowledgeSuggestion,
    TaskStatus,
)

if TYPE_CHECKING:
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager

log = structlog.get_logger()


class TaskManager:
    """Manages task creation and knowledge integration."""

    def __init__(
        self, entity_manager: "EntityManager", relationship_manager: "RelationshipManager"
    ) -> None:
        """Initialize task manager with graph managers."""
        self._entity_manager = entity_manager
        self._relationship_manager = relationship_manager

    async def create_task_with_knowledge_links(
        self, task: Task, auto_link_threshold: float = 0.75
    ) -> str:
        """Create task and automatically link to relevant knowledge.

        Args:
            task: Task to create
            auto_link_threshold: Minimum similarity score for auto-linking

        Returns:
            Task UUID
        """
        log.info("Creating task with knowledge links", title=task.title)

        # Create task entity
        task_id = await self._entity_manager.create(task)

        # Build search query from task
        search_query = f"{task.title} {task.description} {' '.join(task.technologies)}"

        # Find related knowledge
        related = await self._entity_manager.search(
            query=search_query,
            entity_types=[
                EntityType.PATTERN,
                EntityType.RULE,
                EntityType.TEMPLATE,
                EntityType.EPISODE,
            ],
            limit=10,
        )

        # Auto-link high-relevance items
        links_created = 0
        for entity, score in related:
            if score >= auto_link_threshold:
                rel_type = self._determine_relationship_type(entity.entity_type)
                await self._relationship_manager.create(
                    Relationship(
                        id=str(uuid.uuid4()),
                        source_id=task_id,
                        target_id=entity.id,
                        relationship_type=rel_type,
                        weight=score,
                        metadata={"auto_created": True, "confidence": score},
                    )
                )
                links_created += 1

        # Link to domain topic if specified
        if task.domain:
            topic_entities = await self._entity_manager.search(
                query=task.domain, entity_types=[EntityType.TOPIC], limit=1
            )
            if topic_entities:
                await self._relationship_manager.create(
                    Relationship(
                        id=str(uuid.uuid4()),
                        source_id=task_id,
                        target_id=topic_entities[0][0].id,
                        relationship_type=RelationshipType.PART_OF,
                        weight=1.0,
                    )
                )
                links_created += 1

        # Link to project
        if task.project_id:
            await self._relationship_manager.create(
                Relationship(
                    id=str(uuid.uuid4()),
                    source_id=task_id,
                    target_id=task.project_id,
                    relationship_type=RelationshipType.BELONGS_TO,
                    weight=1.0,
                )
            )

        log.info("Task created with knowledge links", task_id=task_id, links_created=links_created)
        return task_id

    async def suggest_task_knowledge(
        self, task_title: str, task_description: str, technologies: list[str], limit: int = 5
    ) -> TaskKnowledgeSuggestion:
        """Suggest relevant knowledge for a new task.

        Args:
            task_title: Task title
            task_description: Task description
            technologies: Technologies involved
            limit: Max suggestions per category

        Returns:
            Knowledge suggestions
        """
        log.info("Suggesting knowledge for task", title=task_title)

        query = f"{task_title} {task_description} {' '.join(technologies)}"

        # Search across all knowledge types in parallel
        patterns, rules, templates, episodes, error_patterns = await asyncio.gather(
            self._entity_manager.search(
                query=query, entity_types=[EntityType.PATTERN], limit=limit
            ),
            self._entity_manager.search(query=query, entity_types=[EntityType.RULE], limit=limit),
            self._entity_manager.search(
                query=query, entity_types=[EntityType.TEMPLATE], limit=limit
            ),
            self._entity_manager.search(
                query=query, entity_types=[EntityType.EPISODE], limit=limit
            ),
            self._entity_manager.search(
                query=query, entity_types=[EntityType.ERROR_PATTERN], limit=limit
            ),
        )

        return TaskKnowledgeSuggestion(
            patterns=[(e.id, s) for e, s in patterns],
            rules=[(e.id, s) for e, s in rules],
            templates=[(e.id, s) for e, s in templates],
            past_learnings=[(e.id, s) for e, s in episodes],
            error_patterns=[(e.id, s) for e, s in error_patterns],
        )

    async def find_similar_tasks(
        self, task: Task, status_filter: list[TaskStatus] | None = None, limit: int = 10
    ) -> list[tuple[Task, float]]:
        """Find tasks similar to the given task.

        Args:
            task: Reference task
            status_filter: Filter by task status
            limit: Max results

        Returns:
            List of (similar_task, similarity_score)
        """
        log.info("Finding similar tasks", task_id=task.id)

        # Search using task content as query
        query = f"{task.title} {task.description} {task.domain or ''}"

        similar = await self._entity_manager.search(
            query=query,
            entity_types=[EntityType.TASK],
            limit=limit * 2,  # Get extra for filtering
        )

        # Filter and convert to Task objects
        results = []
        for entity, score in similar:
            # Skip self
            if entity.id == task.id:
                continue

            # Convert to Task (simplified)
            task_entity = self._entity_to_task(entity)

            # Filter by status
            if status_filter and task_entity.status not in status_filter:
                continue

            results.append((task_entity, score))

            if len(results) >= limit:
                break

        log.info("Found similar tasks", count=len(results))
        return results

    async def estimate_task_effort(self, task: Task) -> TaskEstimate:
        """Estimate task effort based on similar completed tasks.

        Args:
            task: Task to estimate

        Returns:
            Effort estimate
        """
        log.info("Estimating task effort", task_id=task.id)

        # Find similar completed tasks
        similar = await self.find_similar_tasks(task, status_filter=[TaskStatus.DONE], limit=20)

        if not similar:
            return TaskEstimate(
                estimated_hours=None, confidence=0.0, reason="No similar completed tasks found"
            )

        # Extract actual hours from similar tasks
        efforts = []
        for similar_task, similarity in similar:
            if similar_task.actual_hours:
                efforts.append(
                    {
                        "hours": similar_task.actual_hours,
                        "weight": similarity,
                        "task_id": similar_task.id,
                        "task_title": similar_task.title,
                    }
                )

        if not efforts:
            return TaskEstimate(
                estimated_hours=None,
                confidence=0.0,
                reason="Similar tasks found but none have time tracking",
            )

        # Weighted average
        total_weight = sum(e["weight"] for e in efforts)
        weighted_avg = sum(e["hours"] * e["weight"] for e in efforts) / total_weight

        # Confidence based on number of samples and avg similarity
        avg_similarity = total_weight / len(efforts)
        confidence = min(1.0, (len(efforts) / 10) * avg_similarity)

        return TaskEstimate(
            estimated_hours=round(weighted_avg, 1),
            confidence=round(confidence, 2),
            based_on_tasks=len(efforts),
            similar_tasks=[
                SimilarTaskInfo(
                    task_id=e["task_id"],
                    title=e["task_title"],
                    actual_hours=e["hours"],
                    similarity_score=e["weight"],
                )
                for e in efforts[:5]  # Top 5
            ],
        )

    async def get_task_dependencies(self, task_id: str) -> list[tuple[Task, str]]:
        """Get tasks that this task depends on.

        Args:
            task_id: Task UUID

        Returns:
            List of (dependency_task, relationship_type)
        """
        log.debug("Getting task dependencies", task_id=task_id)

        # Get dependency relationships
        relationships = await self._relationship_manager.get_for_entity(
            task_id, relationship_types=[RelationshipType.DEPENDS_ON], direction="outgoing"
        )

        if not relationships:
            return []

        # Fetch all task entities in parallel
        entities = await asyncio.gather(
            *[self._entity_manager.get(rel.target_id) for rel in relationships],
            return_exceptions=True,
        )

        # Build result, skipping any failed fetches
        dependencies = []
        for rel, entity in zip(relationships, entities, strict=True):
            if isinstance(entity, BaseException):
                log.warning(
                    "Failed to fetch dependency", target_id=rel.target_id, error=str(entity)
                )
                continue
            dep_task = self._entity_to_task(entity)
            dependencies.append((dep_task, rel.relationship_type.value))

        return dependencies

    async def get_blocking_tasks(self, task_id: str) -> list[Task]:
        """Get tasks that are blocked by this task.

        Args:
            task_id: Task UUID

        Returns:
            List of tasks blocked by this task
        """
        log.debug("Getting blocked tasks", task_id=task_id)

        # Get incoming DEPENDS_ON relationships (tasks that depend on this one)
        relationships = await self._relationship_manager.get_for_entity(
            task_id, relationship_types=[RelationshipType.DEPENDS_ON], direction="incoming"
        )

        if not relationships:
            return []

        # Fetch all task entities in parallel
        entities = await asyncio.gather(
            *[self._entity_manager.get(rel.source_id) for rel in relationships],
            return_exceptions=True,
        )

        # Build result, skipping any failed fetches
        blocked_tasks = []
        for rel, entity in zip(relationships, entities, strict=True):
            if isinstance(entity, BaseException):
                log.warning(
                    "Failed to fetch blocked task", source_id=rel.source_id, error=str(entity)
                )
                continue
            blocked_task = self._entity_to_task(entity)
            blocked_tasks.append(blocked_task)

        return blocked_tasks

    def _determine_relationship_type(self, entity_type: EntityType) -> RelationshipType:
        """Determine appropriate relationship type for task-knowledge link.

        Args:
            entity_type: Type of knowledge entity

        Returns:
            Relationship type to use
        """
        mapping = {
            EntityType.PATTERN: RelationshipType.REFERENCES,
            EntityType.RULE: RelationshipType.REQUIRES,
            EntityType.TEMPLATE: RelationshipType.REFERENCES,
            EntityType.EPISODE: RelationshipType.REFERENCES,
            EntityType.ERROR_PATTERN: RelationshipType.REFERENCES,
        }
        return mapping.get(entity_type, RelationshipType.RELATED_TO)

    def _entity_to_task(self, entity: Entity) -> Task:
        """Convert Entity to Task model.

        Args:
            entity: Entity from graph

        Returns:
            Task instance
        """
        # This is simplified - real implementation would properly deserialize
        # task-specific fields from entity.metadata
        return Task(
            id=entity.id,
            entity_type=EntityType.TASK,
            title=entity.name,
            description=entity.description,
            name=entity.name,
            content=entity.content,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            # Task-specific fields from metadata
            status=TaskStatus(entity.metadata.get("status", "todo")),
            priority=entity.metadata.get("priority", "medium"),
            task_order=entity.metadata.get("task_order", 0),
            project_id=entity.metadata.get("project_id"),
            feature=entity.metadata.get("feature"),
            sprint=entity.metadata.get("sprint"),
            assignees=entity.metadata.get("assignees", []),
            due_date=entity.metadata.get("due_date"),
            estimated_hours=entity.metadata.get("estimated_hours"),
            actual_hours=entity.metadata.get("actual_hours"),
            domain=entity.metadata.get("domain"),
            technologies=entity.metadata.get("technologies", []),
            complexity=entity.metadata.get("complexity", "medium"),
            branch_name=entity.metadata.get("branch_name"),
            commit_shas=entity.metadata.get("commit_shas", []),
            pr_url=entity.metadata.get("pr_url"),
            learnings=entity.metadata.get("learnings", ""),
            blockers_encountered=entity.metadata.get("blockers_encountered", []),
            started_at=entity.metadata.get("started_at"),
            completed_at=entity.metadata.get("completed_at"),
            reviewed_at=entity.metadata.get("reviewed_at"),
        )
