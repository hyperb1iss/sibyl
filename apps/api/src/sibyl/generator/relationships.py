"""Relationship weaving for generated entities."""

import uuid
from collections import defaultdict

from sibyl.generator.config import GeneratorConfig
from sibyl.models.entities import Entity, EntityType, Relationship, RelationshipType


class RelationshipWeaver:
    """Weave realistic relationships between generated entities.

    Creates:
    - Tasks → Projects (BELONGS_TO)
    - Tasks → Tasks (DEPENDS_ON)
    - Patterns → Tasks (REFERENCES - actually RELATED_TO)
    - Episodes → Tasks (DERIVED_FROM)
    - Rules → Patterns (ENABLES)
    """

    def __init__(self, config: GeneratorConfig) -> None:
        self.config = config
        self._rng = None
        self._rel_counter = 0

    @property
    def rng(self):
        """Get seeded random number generator."""
        if self._rng is None:
            import random

            self._rng = random.Random(self.config.seed)  # noqa: S311 - deterministic seed for reproducibility
        return self._rng

    def _next_id(self) -> str:
        """Generate a unique relationship ID."""
        self._rel_counter += 1
        return f"rel_{uuid.uuid4().hex[:8]}_{self._rel_counter:06d}"

    def weave(self, entities: list[Entity]) -> list[Relationship]:
        """Weave relationships between entities.

        Args:
            entities: List of entities to connect.

        Returns:
            List of relationships.
        """
        # Index entities by type
        by_type: dict[EntityType, list[Entity]] = defaultdict(list)
        for entity in entities:
            by_type[entity.entity_type].append(entity)

        relationships = []

        # Tasks → Projects (BELONGS_TO)
        relationships.extend(
            self._weave_task_projects(by_type[EntityType.TASK], by_type[EntityType.PROJECT])
        )

        # Tasks → Tasks (DEPENDS_ON)
        relationships.extend(self._weave_task_dependencies(by_type[EntityType.TASK]))

        # Patterns → Tasks (RELATED_TO)
        relationships.extend(
            self._weave_pattern_references(by_type[EntityType.PATTERN], by_type[EntityType.TASK])
        )

        # Episodes → Tasks (DERIVED_FROM)
        relationships.extend(
            self._weave_episode_sources(by_type[EntityType.EPISODE], by_type[EntityType.TASK])
        )

        # Rules → Patterns (ENABLES)
        relationships.extend(
            self._weave_rule_patterns(by_type[EntityType.RULE], by_type[EntityType.PATTERN])
        )

        return relationships

    def _weave_task_projects(
        self,
        tasks: list[Entity],
        projects: list[Entity],
    ) -> list[Relationship]:
        """Connect tasks to their projects."""
        if not projects:
            return []

        relationships = []
        for task in tasks:
            # Check if task already has a project_id in metadata
            project_id = task.metadata.get("project_id") if task.metadata else None

            if project_id:
                # Verify project exists
                project_exists = any(p.id == project_id for p in projects)
                if project_exists:
                    relationships.append(
                        Relationship(
                            id=self._next_id(),
                            source_id=task.id,
                            target_id=project_id,
                            relationship_type=RelationshipType.BELONGS_TO,
                            metadata={"_generated": True},
                        )
                    )
            else:
                # Assign to random project
                project = self.rng.choice(projects)
                relationships.append(
                    Relationship(
                        id=self._next_id(),
                        source_id=task.id,
                        target_id=project.id,
                        relationship_type=RelationshipType.BELONGS_TO,
                        metadata={"_generated": True},
                    )
                )

        return relationships

    def _weave_task_dependencies(self, tasks: list[Entity]) -> list[Relationship]:
        """Create task dependency chains.

        Uses config.dependency_density to determine what percentage
        of tasks should have dependencies.
        """
        if len(tasks) < 2:
            return []

        relationships = []
        dep_count = int(len(tasks) * self.config.dependency_density)

        # Select random tasks to have dependencies
        dependent_tasks = self.rng.sample(tasks, min(dep_count, len(tasks)))

        for task in dependent_tasks:
            # Find potential dependencies (tasks created before this one)
            potential_deps = [
                t
                for t in tasks
                if t.id != task.id
                and t.created_at
                and task.created_at
                and t.created_at < task.created_at
            ]

            if not potential_deps:
                # Just pick any other task
                potential_deps = [t for t in tasks if t.id != task.id]

            if potential_deps:
                # Add 1-3 dependencies
                num_deps = min(self.rng.randint(1, 3), len(potential_deps))
                deps = self.rng.sample(potential_deps, num_deps)

                for dep in deps:
                    # Check for cycles
                    if not self._would_create_cycle(relationships, task.id, dep.id):
                        relationships.append(
                            Relationship(
                                id=self._next_id(),
                                source_id=task.id,
                                target_id=dep.id,
                                relationship_type=RelationshipType.DEPENDS_ON,
                                metadata={
                                    "_generated": True,
                                    "blocking": self.rng.random() > 0.7,
                                },
                            )
                        )

        return relationships

    def _would_create_cycle(
        self,
        existing: list[Relationship],
        source: str,
        target: str,
    ) -> bool:
        """Check if adding source→target would create a dependency cycle."""
        # Build adjacency map
        deps: dict[str, set[str]] = defaultdict(set)
        for rel in existing:
            if rel.relationship_type == RelationshipType.DEPENDS_ON:
                deps[rel.source_id].add(rel.target_id)

        # Check if target can reach source (would mean cycle)
        visited = set()
        queue = [target]

        while queue:
            current = queue.pop(0)
            if current == source:
                return True
            if current in visited:
                continue
            visited.add(current)
            queue.extend(deps.get(current, []))

        return False

    def _weave_pattern_references(
        self,
        patterns: list[Entity],
        tasks: list[Entity],
    ) -> list[Relationship]:
        """Connect patterns to tasks that reference them."""
        if not patterns or not tasks:
            return []

        relationships = []
        ref_count = int(len(tasks) * self.config.pattern_reference_density)

        # Select tasks that will reference patterns
        referencing_tasks = self.rng.sample(tasks, min(ref_count, len(tasks)))

        for task in referencing_tasks:
            # Find patterns in the same domain
            task_feature = task.metadata.get("feature", "") if task.metadata else ""

            # Match patterns by domain similarity
            matching_patterns = [
                p
                for p in patterns
                if p.metadata and task_feature.lower() in p.metadata.get("domain", "").lower()
            ]

            if not matching_patterns:
                matching_patterns = patterns  # Fallback to any pattern

            # Add 1-2 pattern references
            num_refs = min(self.rng.randint(1, 2), len(matching_patterns))
            refs = self.rng.sample(matching_patterns, num_refs)

            relationships.extend(
                Relationship(
                    id=self._next_id(),
                    source_id=task.id,
                    target_id=pattern.id,
                    relationship_type=RelationshipType.RELATED_TO,
                    metadata={
                        "_generated": True,
                        "context": "implementation",
                    },
                )
                for pattern in refs
            )

        return relationships

    def _weave_episode_sources(
        self,
        episodes: list[Entity],
        tasks: list[Entity],
    ) -> list[Relationship]:
        """Connect episodes to the tasks they were derived from."""
        if not episodes or not tasks:
            return []

        relationships = []

        for episode in episodes:
            # Each episode is derived from 1-3 tasks
            num_sources = min(self.rng.randint(1, 3), len(tasks))
            source_tasks = self.rng.sample(tasks, num_sources)

            relationships.extend(
                Relationship(
                    id=self._next_id(),
                    source_id=episode.id,
                    target_id=task.id,
                    relationship_type=RelationshipType.DERIVED_FROM,
                    metadata={
                        "_generated": True,
                        "learning_type": self.rng.choice(
                            [
                                "implementation",
                                "debugging",
                                "review",
                                "retrospective",
                            ]
                        ),
                    },
                )
                for task in source_tasks
            )

        return relationships

    def _weave_rule_patterns(
        self,
        rules: list[Entity],
        patterns: list[Entity],
    ) -> list[Relationship]:
        """Connect rules to the patterns they enforce."""
        if not rules or not patterns:
            return []

        relationships = []

        for rule in rules:
            # Find patterns in the same domain
            rule_domain = rule.metadata.get("domain", "") if rule.metadata else ""

            matching_patterns = [
                p for p in patterns if p.metadata and p.metadata.get("domain", "") == rule_domain
            ]

            if not matching_patterns:
                # Pick a random pattern
                matching_patterns = [self.rng.choice(patterns)]

            relationships.extend(
                Relationship(
                    id=self._next_id(),
                    source_id=rule.id,
                    target_id=pattern.id,
                    relationship_type=RelationshipType.ENABLES,
                    metadata={
                        "_generated": True,
                        "severity": rule.metadata.get("severity", "warning")
                        if rule.metadata
                        else "warning",
                    },
                )
                for pattern in matching_patterns
            )

        return relationships
