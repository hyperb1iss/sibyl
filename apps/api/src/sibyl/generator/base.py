"""Base classes for generators."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sibyl.generator.config import GeneratorConfig
from sibyl.models.entities import Entity, Relationship

if TYPE_CHECKING:
    from random import Random


@dataclass
class GeneratorResult:
    """Result of a generation run."""

    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Check if generation was successful."""
        return len(self.errors) == 0

    @property
    def entity_count(self) -> int:
        """Total entities generated."""
        return len(self.entities)

    @property
    def relationship_count(self) -> int:
        """Total relationships generated."""
        return len(self.relationships)

    def merge(self, other: "GeneratorResult") -> "GeneratorResult":
        """Merge another result into this one."""
        return GeneratorResult(
            entities=self.entities + other.entities,
            relationships=self.relationships + other.relationships,
            duration_seconds=self.duration_seconds + other.duration_seconds,
            errors=self.errors + other.errors,
        )


class BaseGenerator(ABC):
    """Abstract base class for all generators."""

    def __init__(self, config: GeneratorConfig) -> None:
        self.config = config
        self._rng: Random | None = None

    @property
    def rng(self) -> "Random":
        """Get seeded random number generator."""
        if self._rng is None:
            import random

            self._rng = random.Random(self.config.seed)  # noqa: S311 - deterministic seed for reproducibility
        return self._rng

    def _generate_id(self, prefix: str = "gen") -> str:
        """Generate a unique ID with optional prefix."""
        import uuid

        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def now(self) -> datetime:
        """Get current UTC timestamp."""
        return datetime.now(tz=UTC)

    def mark_generated(self, metadata: dict[str, object]) -> dict[str, object]:
        """Add generation marker to metadata."""
        metadata["_generated"] = True
        metadata["_generator"] = self.__class__.__name__
        metadata["_generated_at"] = self.now().isoformat()
        if self.config.seed:
            metadata["_seed"] = self.config.seed
        return metadata

    @abstractmethod
    async def generate(self) -> GeneratorResult:
        """Generate entities and relationships.

        Returns:
            GeneratorResult with generated entities and any errors.
        """
        ...

    @abstractmethod
    async def generate_batch(self, count: int, entity_type: str) -> list[Entity]:
        """Generate a batch of entities of a specific type.

        Args:
            count: Number of entities to generate.
            entity_type: Type of entity to generate.

        Returns:
            List of generated entities.
        """
        ...


# Import Random type for annotation
