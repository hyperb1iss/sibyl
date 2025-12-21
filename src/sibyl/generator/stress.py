"""Stress test generator for maximum-scale data generation."""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sibyl.generator.base import BaseGenerator, GeneratorResult
from sibyl.generator.config import GeneratorConfig, StressConfig
from sibyl.models.entities import Entity, EntityType, Relationship, RelationshipType

# Map string types to EntityType enum
ENTITY_TYPE_MAP: dict[str, EntityType] = {
    "task": EntityType.TASK,
    "pattern": EntityType.PATTERN,
    "episode": EntityType.EPISODE,
    "rule": EntityType.RULE,
    "template": EntityType.TEMPLATE,
    "project": EntityType.PROJECT,
    "topic": EntityType.TOPIC,
    "tool": EntityType.TOOL,
}

# Entity type templates for stress testing
STRESS_TEMPLATES = {
    "task": {
        "titles": [
            "Implement feature {n}",
            "Fix bug in module {n}",
            "Refactor component {n}",
            "Add tests for service {n}",
            "Optimize query {n}",
            "Update documentation {n}",
            "Review PR {n}",
            "Deploy service {n}",
        ],
        "statuses": ["backlog", "todo", "doing", "blocked", "review", "done"],
    },
    "pattern": {
        "names": [
            "Pattern Alpha-{n}",
            "Design Pattern {n}",
            "Architecture Pattern {n}",
            "Code Pattern {n}",
            "Integration Pattern {n}",
        ],
        "domains": ["API", "Database", "Security", "Performance", "Testing", "CI/CD"],
    },
    "episode": {
        "names": [
            "Learning from incident {n}",
            "Discovery during development {n}",
            "Insight from review {n}",
            "Experience with deployment {n}",
        ],
        "impacts": ["high", "medium", "low"],
    },
    "rule": {
        "names": [
            "Rule {n}: Input validation",
            "Rule {n}: Error handling",
            "Rule {n}: Logging standard",
            "Rule {n}: Naming convention",
        ],
        "severities": ["error", "warning", "info"],
    },
    "template": {
        "names": [
            "Template {n}: API endpoint",
            "Template {n}: Service class",
            "Template {n}: Test file",
            "Template {n}: Migration",
        ],
        "categories": ["backend", "frontend", "infrastructure", "testing"],
    },
    "project": {
        "names": [
            "Project Alpha-{n}",
            "Initiative {n}",
            "Sprint {n}",
            "Milestone {n}",
        ],
        "statuses": ["active", "planning", "completed", "on-hold"],
    },
    "topic": {
        "names": [
            "Topic: {n} Architecture",
            "Topic: {n} Best Practices",
            "Topic: {n} Patterns",
        ],
    },
    "tool": {
        "names": [
            "Tool {n}: Linter",
            "Tool {n}: Formatter",
            "Tool {n}: Analyzer",
            "Tool {n}: Generator",
        ],
    },
}


class StressTestGenerator(BaseGenerator):
    """Generate maximum-scale data for stress testing.

    Optimized for speed - uses minimal templates and parallel generation.
    """

    def __init__(self, stress_config: StressConfig, seed: int | None = None) -> None:
        # Create a minimal GeneratorConfig for the base class
        config = GeneratorConfig(seed=seed)
        super().__init__(config)
        self.stress_config = stress_config
        self._counter = 0
        self._rel_counter = 0

    def _quick_id(self, prefix: str) -> str:
        """Generate fast sequential ID."""
        self._counter += 1
        return f"{prefix}_{self._counter:08d}"

    def _quick_uuid(self) -> str:
        """Generate UUID without dashes for compactness."""
        return uuid.uuid4().hex

    def _next_rel_id(self) -> str:
        """Generate a unique relationship ID."""
        self._rel_counter += 1
        return f"rel_{uuid.uuid4().hex[:8]}_{self._rel_counter:06d}"

    async def generate(self) -> GeneratorResult:
        """Generate stress test data as fast as possible."""
        start_time = time.time()
        result = GeneratorResult()

        # Calculate entity counts by type
        type_counts: dict[str, int] = {}
        remaining = self.stress_config.entities

        for entity_type, percentage in self.stress_config.type_distribution.items():
            count = int(self.stress_config.entities * percentage)
            type_counts[entity_type] = count
            remaining -= count

        # Distribute remaining to largest type
        if remaining > 0:
            largest_type = max(type_counts, key=lambda k: type_counts[k])
            type_counts[largest_type] += remaining

        # Generate entities by type
        all_entities = []
        for entity_type, count in type_counts.items():
            batch = await self._generate_type_batch(entity_type, count)
            all_entities.extend(batch)

        result.entities = all_entities

        # Generate relationships
        relationships = await self._generate_stress_relationships(all_entities)
        result.relationships = relationships

        result.duration_seconds = time.time() - start_time
        return result

    async def generate_batch(self, count: int, entity_type: str) -> list[Entity]:
        """Generate a batch of entities of a specific type."""
        return await self._generate_type_batch(entity_type, count)

    async def _generate_type_batch(self, entity_type: str, count: int) -> list[Entity]:
        """Generate a batch of entities of a specific type quickly."""
        templates = STRESS_TEMPLATES.get(entity_type, {})
        names = templates.get("names", [f"{entity_type.title()} {{n}}"])

        entities = []
        base_time = datetime.now(tz=UTC)

        for i in range(count):
            name_template = names[i % len(names)]
            name = name_template.format(n=i + 1)

            # Build minimal metadata
            metadata = {
                "_generated": True,
                "_stress_test": True,
                "_batch_index": i,
            }

            # Add type-specific metadata
            if entity_type == "task":
                metadata["status"] = templates["statuses"][i % len(templates["statuses"])]
                metadata["priority"] = ["critical", "high", "medium", "low"][i % 4]
            elif entity_type == "pattern":
                metadata["domain"] = templates["domains"][i % len(templates["domains"])]
            elif entity_type == "episode":
                metadata["impact"] = templates["impacts"][i % len(templates["impacts"])]
            elif entity_type == "rule":
                metadata["severity"] = templates["severities"][i % len(templates["severities"])]
            elif entity_type == "template":
                metadata["category"] = templates["categories"][i % len(templates["categories"])]
            elif entity_type == "project":
                metadata["status"] = templates["statuses"][i % len(templates["statuses"])]

            # Vary creation time for realistic distribution
            created_at = base_time - timedelta(
                days=self.rng.randint(0, 365),
                hours=self.rng.randint(0, 23),
                minutes=self.rng.randint(0, 59),
            )

            entities.append(
                Entity(
                    id=self._quick_id(entity_type[:4]),
                    name=name,
                    entity_type=ENTITY_TYPE_MAP.get(entity_type, EntityType.PATTERN),
                    description=f"Stress test {entity_type} entity #{i + 1}",
                    content=f"# {name}\n\nGenerated content for stress testing.",
                    metadata=metadata,
                    created_at=created_at,
                )
            )

        return entities

    async def _generate_stress_relationships(
        self,
        entities: list[Entity],
    ) -> list[Relationship]:
        """Generate relationships optimized for stress testing."""
        relationships = []
        target_count = self.stress_config.relationships

        # Index entities by type for faster lookup
        by_type: dict[str, list[Entity]] = {}
        for entity in entities:
            by_type.setdefault(str(entity.entity_type), []).append(entity)

        # Relationship types and their source→target mappings
        rel_types: list[tuple[RelationshipType, str | None, str | None]] = [
            (RelationshipType.BELONGS_TO, "task", "project"),
            (RelationshipType.DEPENDS_ON, "task", "task"),
            (RelationshipType.RELATED_TO, "task", "pattern"),
            (RelationshipType.DERIVED_FROM, "episode", "task"),
            (RelationshipType.ENABLES, "rule", "pattern"),
            (RelationshipType.RELATED_TO, None, None),  # Any to any
        ]

        count = 0
        max_iterations = target_count * 2  # Prevent infinite loop
        iterations = 0

        while count < target_count and iterations < max_iterations:
            iterations += 1

            # Pick relationship type
            rel_type, source_type, target_type = self.rng.choice(rel_types)

            # Get source and target entities
            if source_type and target_type:
                sources = by_type.get(source_type, [])
                targets = by_type.get(target_type, [])
            else:
                # RELATED_TO can be any type
                sources = entities
                targets = entities

            if not sources or not targets:
                continue

            source = self.rng.choice(sources)
            target = self.rng.choice(targets)

            # Avoid self-references
            if source.id == target.id:
                continue

            relationships.append(
                Relationship(
                    id=self._next_rel_id(),
                    source_id=source.id,
                    target_id=target.id,
                    relationship_type=rel_type,
                    metadata={
                        "_generated": True,
                        "_stress_test": True,
                    },
                )
            )
            count += 1

        return relationships

    async def run_with_progress(
        self,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> GeneratorResult:
        """Run generation with progress reporting.

        Args:
            progress_callback: Optional callback(step: str, current: int, total: int)
        """
        start_time = time.time()
        total = self.stress_config.entities + self.stress_config.relationships

        if progress_callback:
            progress_callback("Starting stress test", 0, total)

        result = await self.generate()

        if progress_callback:
            progress_callback(
                "Complete",
                result.entity_count + result.relationship_count,
                total,
            )

        result.duration_seconds = time.time() - start_time
        return result
