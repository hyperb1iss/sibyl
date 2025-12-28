"""Generator configuration and types."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ModelType(str, Enum):
    """Supported LLM models for content generation."""

    SONNET = "sonnet"  # Claude Sonnet 4.5 - balanced quality/cost
    OPUS = "opus"  # Claude Opus 4.5 - highest quality

    @property
    def model_id(self) -> str:
        """Get the full Anthropic model ID."""
        return {
            ModelType.SONNET: "claude-sonnet-4-5-20250929",
            ModelType.OPUS: "claude-opus-4-5-20251101",
        }[self]


@dataclass
class GeneratorConfig:
    """Configuration for data generation."""

    # Counts
    projects: int = 5
    tasks_per_project: int = 20
    patterns: int = 50
    episodes: int = 100
    rules: int = 30
    templates: int = 20

    # Generation settings
    seed: int | None = None
    model: ModelType = ModelType.SONNET
    use_llm: bool = True  # False = template-only mode

    # Relationship density (0.0 - 1.0)
    dependency_density: float = 0.3  # 30% of tasks have dependencies
    pattern_reference_density: float = 0.5  # 50% of tasks reference patterns

    # Tech stack options
    languages: list[str] = field(
        default_factory=lambda: ["Python", "TypeScript", "Rust", "Go", "Java"]
    )
    frameworks: list[str] = field(
        default_factory=lambda: [
            "FastAPI",
            "React",
            "Next.js",
            "Django",
            "Express",
            "Axum",
            "Spring Boot",
        ]
    )
    domains: list[str] = field(
        default_factory=lambda: [
            "API Design",
            "Authentication",
            "Database",
            "Testing",
            "CI/CD",
            "Observability",
            "Security",
            "Performance",
        ]
    )

    # Metadata
    generated_marker: str = "GENERATED_BY_SIBYL"

    @property
    def total_tasks(self) -> int:
        """Total number of tasks to generate."""
        return self.projects * self.tasks_per_project


@dataclass
class ScenarioConfig:
    """Configuration for a predefined scenario."""

    name: str
    description: str
    projects: int
    tasks_per_project: int
    patterns: int
    episodes: int
    rules: int = 20
    templates: int = 10
    dependency_density: float = 0.3
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_generator_config(self, model: ModelType = ModelType.SONNET) -> GeneratorConfig:
        """Convert scenario to generator config."""
        return GeneratorConfig(
            projects=self.projects,
            tasks_per_project=self.tasks_per_project,
            patterns=self.patterns,
            episodes=self.episodes,
            rules=self.rules,
            templates=self.templates,
            dependency_density=self.dependency_density,
            languages=self.languages or GeneratorConfig().languages,
            frameworks=self.frameworks or GeneratorConfig().frameworks,
            domains=self.domains or GeneratorConfig().domains,
            model=model,
        )


@dataclass
class StressConfig:
    """Configuration for stress testing."""

    entities: int = 5000
    relationships: int = 10000
    max_depth: int = 5
    batch_size: int = 100  # Entities per batch for progress reporting

    # Entity type distribution (percentages)
    type_distribution: dict[str, float] = field(
        default_factory=lambda: {
            "task": 0.35,
            "pattern": 0.20,
            "episode": 0.20,
            "rule": 0.10,
            "template": 0.05,
            "project": 0.05,
            "topic": 0.03,
            "tool": 0.02,
        }
    )
