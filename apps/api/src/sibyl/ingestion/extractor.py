"""Entity extractor for identifying patterns, rules, and concepts in episodes."""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from sibyl.ingestion.chunker import Episode
from sibyl_core.models.entities import EntityType


class ExtractedEntityType(StrEnum):
    """Types of entities that can be extracted from text."""

    PATTERN = "pattern"
    RULE = "rule"
    TOOL = "tool"
    LANGUAGE = "language"
    CONCEPT = "concept"
    WARNING = "warning"
    TIP = "tip"


@dataclass
class ExtractedEntity:
    """An entity extracted from episode content."""

    entity_type: ExtractedEntityType
    name: str
    description: str
    confidence: float  # 0.0 to 1.0
    source_episode_id: str
    context: str  # Surrounding text
    metadata: dict[str, object] = field(default_factory=dict)

    def to_entity_type(self) -> EntityType:
        """Convert to canonical EntityType."""
        mapping = {
            ExtractedEntityType.PATTERN: EntityType.PATTERN,
            ExtractedEntityType.RULE: EntityType.RULE,
            ExtractedEntityType.TOOL: EntityType.TOOL,
            ExtractedEntityType.LANGUAGE: EntityType.LANGUAGE,
            ExtractedEntityType.CONCEPT: EntityType.TOPIC,
            ExtractedEntityType.WARNING: EntityType.RULE,
            ExtractedEntityType.TIP: EntityType.PATTERN,
        }
        return mapping.get(self.entity_type, EntityType.TOPIC)


class EntityExtractor:
    """Extracts structured entities from episode content.

    Uses pattern matching and heuristics to identify:
    - Programming languages mentioned
    - Tools and libraries referenced
    - Patterns described
    - Rules and warnings stated
    """

    # Known programming languages
    LANGUAGES = {
        "python",
        "typescript",
        "javascript",
        "rust",
        "swift",
        "go",
        "java",
        "kotlin",
        "ruby",
        "php",
        "c",
        "c++",
        "c#",
        "scala",
        "haskell",
        "elixir",
        "clojure",
        "lua",
        "perl",
        "r",
        "julia",
        "dart",
        "zig",
    }

    # Common tools and libraries
    TOOLS = {
        "ruff",
        "mypy",
        "pytest",
        "eslint",
        "prettier",
        "docker",
        "kubernetes",
        "git",
        "npm",
        "pnpm",
        "yarn",
        "cargo",
        "pip",
        "uv",
        "poetry",
        "webpack",
        "vite",
        "next.js",
        "react",
        "vue",
        "angular",
        "fastapi",
        "django",
        "flask",
        "express",
        "graphiti",
        "neo4j",
        "redis",
        "postgres",
        "falkordb",
        "supabase",
        "vercel",
        "cloudflare",
        "aws",
        "gcp",
        "azure",
    }

    # Patterns indicating rules
    RULE_PATTERNS = [
        (re.compile(r"(?:never|always|must|shall|should not)\s+(.+)", re.IGNORECASE), 0.9),
        (re.compile(r"(?:do not|don't|avoid)\s+(.+)", re.IGNORECASE), 0.85),
        (re.compile(r"(?:rule|invariant|requirement):\s*(.+)", re.IGNORECASE), 0.95),
        (re.compile(r"(?:sacred|inviolable|critical):\s*(.+)", re.IGNORECASE), 0.95),
    ]

    # Patterns indicating tips/best practices
    TIP_PATTERNS = [
        (re.compile(r"(?:tip|best practice|recommendation):\s*(.+)", re.IGNORECASE), 0.85),
        (re.compile(r"(?:prefer|consider|try to)\s+(.+)", re.IGNORECASE), 0.7),
        (re.compile(r"(?:pro tip|hint):\s*(.+)", re.IGNORECASE), 0.8),
    ]

    # Patterns indicating warnings
    WARNING_PATTERNS = [
        (re.compile(r"(?:warning|caution|beware):\s*(.+)", re.IGNORECASE), 0.9),
        (re.compile(r"(?:gotcha|pitfall|trap):\s*(.+)", re.IGNORECASE), 0.85),
        (re.compile(r"(?:watch out|be careful)\s+(.+)", re.IGNORECASE), 0.8),
    ]

    def __init__(self) -> None:
        """Initialize the extractor."""
        self._language_pattern = re.compile(
            r"\b(" + "|".join(re.escape(lang) for lang in self.LANGUAGES) + r")\b",
            re.IGNORECASE,
        )
        self._tool_pattern = re.compile(
            r"\b(" + "|".join(re.escape(tool) for tool in self.TOOLS) + r")\b",
            re.IGNORECASE,
        )

    def extract_from_episode(self, episode: Episode) -> list[ExtractedEntity]:
        """Extract all entities from an episode.

        Args:
            episode: Episode to extract from.

        Returns:
            List of extracted entities.
        """
        entities: list[ExtractedEntity] = []

        # Extract languages
        entities.extend(self._extract_languages(episode))

        # Extract tools
        entities.extend(self._extract_tools(episode))

        # Extract rules
        entities.extend(self._extract_rules(episode))

        # Extract tips
        entities.extend(self._extract_tips(episode))

        # Extract warnings
        entities.extend(self._extract_warnings(episode))

        return entities

    def _extract_languages(self, episode: Episode) -> list[ExtractedEntity]:
        """Extract programming language mentions."""
        entities = []
        content = episode.content.lower()

        for match in self._language_pattern.finditer(content):
            lang = match.group(1).lower()
            # Get context (surrounding 50 chars)
            start = max(0, match.start() - 50)
            end = min(len(content), match.end() + 50)
            context = content[start:end]

            entities.append(
                ExtractedEntity(
                    entity_type=ExtractedEntityType.LANGUAGE,
                    name=lang.title(),
                    description=f"Programming language mentioned in {episode.title}",
                    confidence=0.95,
                    source_episode_id=episode.id,
                    context=context,
                )
            )

        # Deduplicate by name
        seen = set()
        unique = []
        for e in entities:
            if e.name.lower() not in seen:
                seen.add(e.name.lower())
                unique.append(e)
        return unique

    def _extract_tools(self, episode: Episode) -> list[ExtractedEntity]:
        """Extract tool and library mentions."""
        entities = []
        content = episode.content.lower()

        for match in self._tool_pattern.finditer(content):
            tool = match.group(1).lower()
            start = max(0, match.start() - 50)
            end = min(len(content), match.end() + 50)
            context = content[start:end]

            entities.append(
                ExtractedEntity(
                    entity_type=ExtractedEntityType.TOOL,
                    name=tool,
                    description=f"Tool/library mentioned in {episode.title}",
                    confidence=0.9,
                    source_episode_id=episode.id,
                    context=context,
                )
            )

        # Deduplicate
        seen = set()
        unique = []
        for e in entities:
            if e.name.lower() not in seen:
                seen.add(e.name.lower())
                unique.append(e)
        return unique

    def _extract_rules(self, episode: Episode) -> list[ExtractedEntity]:
        """Extract rules and invariants."""
        return self._extract_by_patterns(
            episode,
            self.RULE_PATTERNS,
            ExtractedEntityType.RULE,
        )

    def _extract_tips(self, episode: Episode) -> list[ExtractedEntity]:
        """Extract tips and best practices."""
        return self._extract_by_patterns(
            episode,
            self.TIP_PATTERNS,
            ExtractedEntityType.TIP,
        )

    def _extract_warnings(self, episode: Episode) -> list[ExtractedEntity]:
        """Extract warnings and gotchas."""
        return self._extract_by_patterns(
            episode,
            self.WARNING_PATTERNS,
            ExtractedEntityType.WARNING,
        )

    def _extract_by_patterns(
        self,
        episode: Episode,
        patterns: list[tuple[re.Pattern[str], float]],
        entity_type: ExtractedEntityType,
    ) -> list[ExtractedEntity]:
        """Extract entities matching given patterns."""
        entities = []
        content = episode.content

        for pattern, confidence in patterns:
            for match in pattern.finditer(content):
                extracted_text = match.group(1).strip() if match.groups() else match.group(0)

                # Skip very short matches
                if len(extracted_text) < 10:
                    continue

                # Truncate very long matches
                if len(extracted_text) > 200:
                    extracted_text = extracted_text[:200] + "..."

                # Get broader context
                start = max(0, match.start() - 100)
                end = min(len(content), match.end() + 100)
                context = content[start:end]

                entities.append(
                    ExtractedEntity(
                        entity_type=entity_type,
                        name=self._generate_name(extracted_text),
                        description=extracted_text,
                        confidence=confidence,
                        source_episode_id=episode.id,
                        context=context,
                    )
                )

        return entities

    def _generate_name(self, text: str) -> str:
        """Generate a short name from extracted text."""
        # Take first 50 chars and clean up
        name = text[:50].strip()
        # Remove trailing incomplete words
        if len(text) > 50:
            last_space = name.rfind(" ")
            if last_space > 20:
                name = name[:last_space]
        return name


def extract_entities_from_episodes(episodes: list[Episode]) -> list[ExtractedEntity]:
    """Extract entities from multiple episodes.

    Args:
        episodes: Episodes to process.

    Returns:
        All extracted entities.
    """
    extractor = EntityExtractor()
    all_entities = []
    for episode in episodes:
        all_entities.extend(extractor.extract_from_episode(episode))
    return all_entities
