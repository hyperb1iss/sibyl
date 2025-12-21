"""Predefined generation scenarios."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from sibyl.generator.base import GeneratorResult
from sibyl.generator.config import ModelType, ScenarioConfig
from sibyl.generator.llm import LLMContentGenerator
from sibyl.generator.relationships import RelationshipWeaver
from sibyl.generator.templates import TemplateGenerator

# Predefined scenarios
SCENARIOS: dict[str, ScenarioConfig] = {
    "startup-mvp": ScenarioConfig(
        name="startup-mvp",
        description="Fast-moving startup building an MVP - 5 projects, 100 tasks, rapid iteration",
        projects=5,
        tasks_per_project=20,
        patterns=30,
        episodes=50,
        rules=15,
        templates=10,
        dependency_density=0.2,  # Fewer dependencies, move fast
        languages=["TypeScript", "Python"],
        frameworks=["Next.js", "FastAPI", "Prisma"],
        domains=["API Design", "Authentication", "Database", "Testing"],
        extra={"iteration_speed": "fast", "tech_debt_tolerance": "high"},
    ),
    "enterprise-migration": ScenarioConfig(
        name="enterprise-migration",
        description="Large enterprise migrating legacy systems - 3 projects, 200 tasks, complex dependencies",
        projects=3,
        tasks_per_project=67,
        patterns=60,
        episodes=80,
        rules=40,
        templates=25,
        dependency_density=0.5,  # Many dependencies due to migration complexity
        languages=["Java", "Python", "Go"],
        frameworks=["Spring Boot", "Django", "gRPC"],
        domains=["Database", "Security", "Observability", "Performance", "CI/CD"],
        extra={"compliance": True, "rollback_required": True},
    ),
    "open-source-library": ScenarioConfig(
        name="open-source-library",
        description="Open source library development - 1 project, 50 tasks, heavy documentation",
        projects=1,
        tasks_per_project=50,
        patterns=80,  # Many patterns for a library
        episodes=30,
        rules=50,  # Strict rules for API consistency
        templates=40,  # Many templates for contributors
        dependency_density=0.3,
        languages=["Rust", "TypeScript"],
        frameworks=["Tokio", "Node.js"],
        domains=["API Design", "Testing", "Performance", "Security"],
        extra={"semver_strict": True, "backwards_compat": True},
    ),
    "data-pipeline": ScenarioConfig(
        name="data-pipeline",
        description="Data engineering pipeline - 2 projects, 75 tasks, heavy on episodes",
        projects=2,
        tasks_per_project=38,
        patterns=40,
        episodes=150,  # Lots of learnings from data issues
        rules=25,
        templates=15,
        dependency_density=0.6,  # Pipelines have many dependencies
        languages=["Python", "SQL", "Scala"],
        frameworks=["Apache Spark", "Airflow", "dbt"],
        domains=["Database", "Performance", "Observability", "CI/CD"],
        extra={"data_quality": True, "idempotent": True},
    ),
    "microservices": ScenarioConfig(
        name="microservices",
        description="Microservices architecture - 8 projects (services), 160 tasks",
        projects=8,
        tasks_per_project=20,
        patterns=50,
        episodes=60,
        rules=35,
        templates=20,
        dependency_density=0.4,
        languages=["Go", "TypeScript", "Python"],
        frameworks=["Gin", "Express", "FastAPI", "gRPC"],
        domains=["API Design", "Authentication", "Observability", "Security", "Performance"],
        extra={"service_mesh": True, "distributed_tracing": True},
    ),
    "mobile-app": ScenarioConfig(
        name="mobile-app",
        description="Cross-platform mobile app - 3 projects, 90 tasks",
        projects=3,
        tasks_per_project=30,
        patterns=35,
        episodes=40,
        rules=20,
        templates=15,
        dependency_density=0.25,
        languages=["TypeScript", "Swift", "Kotlin"],
        frameworks=["React Native", "Expo", "SwiftUI"],
        domains=["API Design", "Authentication", "Performance", "Testing"],
        extra={"offline_first": True, "push_notifications": True},
    ),
}


@dataclass
class ScenarioRunner:
    """Run a predefined scenario to generate data."""

    scenario: ScenarioConfig
    model: ModelType = ModelType.SONNET
    use_llm: bool = True
    seed: int | None = None

    async def run(
        self, progress_callback: Callable[[str, int, int], None] | None = None
    ) -> GeneratorResult:
        """Execute the scenario and generate all data.

        Args:
            progress_callback: Optional callback(step: str, current: int, total: int)

        Returns:
            GeneratorResult with all generated entities and relationships.
        """
        start_time = time.time()

        # Convert scenario to config
        config = self.scenario.to_generator_config(self.model)
        config.seed = self.seed
        config.use_llm = self.use_llm

        # Select generator based on LLM setting
        if self.use_llm:
            generator = LLMContentGenerator(config)
        else:
            generator = TemplateGenerator(config)

        # Progress tracking
        total_entities = (
            config.projects
            + config.total_tasks
            + config.patterns
            + config.episodes
            + config.rules
            + config.templates
        )

        if progress_callback:
            progress_callback("Initializing", 0, total_entities)

        # Generate entities
        result = await generator.generate()

        if progress_callback:
            progress_callback("Weaving relationships", result.entity_count, total_entities)

        # Weave relationships
        weaver = RelationshipWeaver(config)
        relationships = weaver.weave(result.entities)
        result.relationships = relationships

        result.duration_seconds = time.time() - start_time

        if progress_callback:
            progress_callback("Complete", total_entities, total_entities)

        return result


def list_scenarios() -> dict[str, str]:
    """Get a dict of scenario name → description."""
    return {name: scenario.description for name, scenario in SCENARIOS.items()}


def get_scenario(name: str) -> ScenarioConfig | None:
    """Get a scenario by name."""
    return SCENARIOS.get(name)
