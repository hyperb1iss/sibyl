"""Semantic chunker for splitting documents into episodes."""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from sibyl.ingestion.parser import ParsedDocument, Section


@dataclass
class Episode:
    """A knowledge episode extracted from a document.

    Episodes are semantic units of knowledge, typically corresponding
    to H2 or H3 sections in the source document.
    """

    id: str
    source_file: Path
    section_path: list[str]  # Hierarchical path like ["Error Handling", "Python Patterns"]
    title: str
    content: str
    word_count: int
    code_blocks: list[dict[str, str]] = field(default_factory=list)
    parent_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def full_path(self) -> str:
        """Get full section path as string."""
        return " > ".join(self.section_path) if self.section_path else self.title


@dataclass
class ChunkedDocument:
    """A document split into episodes."""

    source_file: Path
    title: str
    episodes: list[Episode]
    frontmatter: dict[str, object]
    total_word_count: int


class SemanticChunker:
    """Splits parsed documents into semantic episodes.

    Chunks at H2/H3 boundaries rather than arbitrary character limits,
    preserving the logical structure of the knowledge.
    """

    def __init__(
        self,
        min_words: int = 50,
        max_words: int = 800,
        target_level: int = 2,
    ) -> None:
        """Initialize the chunker.

        Args:
            min_words: Minimum words per episode (combine smaller sections).
            max_words: Maximum words per episode (split larger sections).
            target_level: Target header level for chunking (2 = H2).
        """
        self.min_words = min_words
        self.max_words = max_words
        self.target_level = target_level

    def chunk_document(self, doc: ParsedDocument) -> ChunkedDocument:
        """Split a parsed document into episodes.

        Args:
            doc: Parsed markdown document.

        Returns:
            ChunkedDocument with episodes.
        """
        episodes: list[Episode] = []
        file_stem = doc.file_path.stem

        for section in doc.sections:
            section_episodes = self._chunk_section(
                section=section,
                source_file=doc.file_path,
                path_prefix=[],
                file_stem=file_stem,
            )
            episodes.extend(section_episodes)

        # Handle case where doc has no sections (just content)
        if not episodes and doc.raw_content.strip():
            episode_id = self._generate_id(file_stem, doc.title)
            episodes.append(
                Episode(
                    id=episode_id,
                    source_file=doc.file_path,
                    section_path=[],
                    title=doc.title,
                    content=doc.raw_content,
                    word_count=doc.word_count,
                    metadata=dict(doc.frontmatter),
                )
            )

        return ChunkedDocument(
            source_file=doc.file_path,
            title=doc.title,
            episodes=episodes,
            frontmatter=doc.frontmatter,
            total_word_count=doc.word_count,
        )

    def _chunk_section(
        self,
        section: Section,
        source_file: Path,
        path_prefix: list[str],
        file_stem: str,
        parent_id: str | None = None,
    ) -> list[Episode]:
        """Recursively chunk a section into episodes.

        Args:
            section: Section to chunk.
            source_file: Source file path.
            path_prefix: Parent section titles.
            file_stem: File stem for ID generation.
            parent_id: Parent episode ID.

        Returns:
            List of episodes from this section.
        """
        episodes: list[Episode] = []
        current_path = [*path_prefix, section.title]
        episode_id = self._generate_id(file_stem, *current_path)

        # Calculate section word count
        section_words = len(section.content.split())

        # Determine if this section should become an episode
        should_create_episode = (
            section.level >= self.target_level  # At or below target level
            or (section.level == 1 and not section.subsections)  # H1 with no children
            or section_words >= self.min_words  # Has substantial content
        )

        if should_create_episode and section.content.strip():
            # Build full content including code blocks context
            content = self._build_episode_content(section)

            # Extract code blocks as structured data
            code_blocks = [
                {"language": cb.language, "content": cb.content} for cb in section.code_blocks
            ]

            episode = Episode(
                id=episode_id,
                source_file=source_file,
                section_path=current_path,
                title=section.title,
                content=content,
                word_count=len(content.split()),
                code_blocks=code_blocks,
                parent_id=parent_id,
            )
            episodes.append(episode)
            parent_id = episode_id

        # Process subsections
        for subsection in section.subsections:
            sub_episodes = self._chunk_section(
                section=subsection,
                source_file=source_file,
                path_prefix=current_path,
                file_stem=file_stem,
                parent_id=parent_id,
            )
            episodes.extend(sub_episodes)

        return episodes

    def _build_episode_content(self, section: Section) -> str:
        """Build the full content for an episode.

        Args:
            section: Section to build content from.

        Returns:
            Full episode content.
        """
        # Start with the section content
        content = section.content

        # If content is minimal but has code blocks, include code context
        if len(content.split()) < self.min_words and section.code_blocks:
            for cb in section.code_blocks:
                if cb.content not in content:
                    content += f"\n\n```{cb.language}\n{cb.content}\n```"

        return content.strip()

    def _generate_id(self, *parts: str) -> str:
        """Generate a deterministic episode ID.

        Args:
            parts: Components to hash for ID.

        Returns:
            Short hash-based ID.
        """
        combined = ":".join(str(p) for p in parts)
        hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
        return f"ep_{hash_bytes}"


def chunk_documents(documents: list[ParsedDocument]) -> list[ChunkedDocument]:
    """Chunk multiple documents into episodes.

    Args:
        documents: List of parsed documents.

    Returns:
        List of chunked documents.
    """
    chunker = SemanticChunker()
    return [chunker.chunk_document(doc) for doc in documents]
