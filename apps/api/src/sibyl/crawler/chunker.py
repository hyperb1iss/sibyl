"""Document chunking pipeline for RAG retrieval.

Implements multiple chunking strategies optimized for different content types:
- Semantic chunking for prose documentation
- AST-aware chunking for code blocks
- Sliding window with overlap for context preservation

Based on research findings:
- Anthropic contextual retrieval: prepend context to chunks
- AST-based code chunking: +4.3% recall improvement
- Sliding window overlap: maintains cross-chunk context
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from sibyl.config import settings
from sibyl.db.models import ChunkType

if TYPE_CHECKING:
    from sibyl.db import CrawledDocument

log = structlog.get_logger()


class ChunkStrategy(StrEnum):
    """Available chunking strategies."""

    SEMANTIC = "semantic"  # Split on semantic boundaries (headers, paragraphs)
    SLIDING = "sliding"  # Fixed-size with overlap
    CODE = "code"  # Code-block aware chunking


@dataclass
class Chunk:
    """A chunk of document content ready for embedding.

    Attributes:
        content: The chunk text content
        context: Optional contextual prefix (Anthropic technique)
        chunk_type: Type of content (text, code, heading, etc.)
        chunk_index: Position in document
        start_char: Start character offset in source
        end_char: End character offset in source
        heading_path: Breadcrumb of headings leading to this chunk
        language: Programming language if code chunk
        token_count: Estimated token count
    """

    content: str
    context: str | None = None
    chunk_type: ChunkType = ChunkType.TEXT
    chunk_index: int = 0
    start_char: int = 0
    end_char: int = 0
    heading_path: list[str] = field(default_factory=list)
    language: str | None = None
    token_count: int = 0

    def __post_init__(self) -> None:
        """Compute token count if not set."""
        if self.token_count == 0:
            # Rough estimate: 1 token ≈ 4 characters
            self.token_count = len(self.content) // 4


class DocumentChunker:
    """Chunks documents using configurable strategies.

    Supports semantic, sliding window, and code-aware chunking.
    Generates contextual prefixes using Anthropic's technique.
    """

    def __init__(
        self,
        *,
        max_chunk_tokens: int | None = None,
        overlap_tokens: int | None = None,
        include_context: bool = True,
    ) -> None:
        """Initialize the chunker.

        Args:
            max_chunk_tokens: Maximum tokens per chunk (default from settings)
            overlap_tokens: Token overlap between chunks (default from settings)
            include_context: Whether to generate contextual prefixes
        """
        self.max_chunk_tokens = max_chunk_tokens or settings.chunk_max_tokens
        self.overlap_tokens = overlap_tokens or settings.chunk_overlap_tokens
        self.include_context = include_context

        # Convert tokens to approximate character counts
        self.max_chunk_chars = self.max_chunk_tokens * 4
        self.overlap_chars = self.overlap_tokens * 4

    def chunk_document(
        self,
        document: CrawledDocument,
        *,
        strategy: ChunkStrategy = ChunkStrategy.SEMANTIC,
    ) -> list[Chunk]:
        """Chunk a document using the specified strategy.

        Args:
            document: Document to chunk
            strategy: Chunking strategy to use

        Returns:
            List of chunks with context and metadata
        """
        content = document.content
        if not content or not content.strip():
            return []

        # Choose chunking method
        if strategy == ChunkStrategy.CODE:
            raw_chunks = self._chunk_code_aware(content)
        elif strategy == ChunkStrategy.SLIDING:
            raw_chunks = self._chunk_sliding_window(content)
        else:  # SEMANTIC (default)
            raw_chunks = self._chunk_semantic(content)

        # Build document context for Anthropic-style contextual retrieval
        doc_context = self._build_document_context(document)

        # Process chunks with context and metadata
        chunks = []
        for i, raw in enumerate(raw_chunks):
            chunk = Chunk(
                content=raw["content"],
                context=self._generate_chunk_context(doc_context, raw)
                if self.include_context
                else None,
                chunk_type=raw.get("type", ChunkType.TEXT),
                chunk_index=i,
                start_char=raw.get("start", 0),
                end_char=raw.get("end", len(raw["content"])),
                heading_path=raw.get("headings", []),
                language=raw.get("language"),
            )
            chunks.append(chunk)

        log.debug(
            "Chunked document",
            url=document.url,
            strategy=strategy,
            chunk_count=len(chunks),
            avg_tokens=sum(c.token_count for c in chunks) // max(len(chunks), 1),
        )

        return chunks

    def _chunk_semantic(self, content: str) -> list[dict]:
        """Chunk content on semantic boundaries.

        Splits on:
        1. Markdown headers (# ## ### etc.)
        2. Double newlines (paragraphs)
        3. Code block boundaries

        Merges small chunks and splits large ones.
        """
        chunks = []
        current_headings: list[str] = []
        current_chunk: list[str] = []
        current_start = 0
        char_pos = 0

        lines = content.split("\n")

        for line in lines:
            line_len = len(line) + 1  # +1 for newline

            # Check for headers
            if line.startswith("#"):
                # Save current chunk if any
                if current_chunk:
                    chunk_text = "\n".join(current_chunk).strip()
                    if chunk_text:
                        chunks.append(
                            {
                                "content": chunk_text,
                                "start": current_start,
                                "end": char_pos,
                                "headings": current_headings.copy(),
                                "type": ChunkType.TEXT,
                            }
                        )
                    current_chunk = []
                    current_start = char_pos

                # Update heading stack
                level = len(line) - len(line.lstrip("#"))
                heading_text = line.lstrip("#").strip()

                # Pop headings at same or lower level
                while current_headings and len(current_headings) >= level:
                    current_headings.pop()
                current_headings.append(heading_text)

                # Add heading as its own chunk
                chunks.append(
                    {
                        "content": heading_text,
                        "start": char_pos,
                        "end": char_pos + line_len,
                        "headings": current_headings.copy(),
                        "type": ChunkType.HEADING,
                    }
                )
                current_start = char_pos + line_len

            # Check for code blocks
            elif line.startswith("```"):
                # Save current chunk
                if current_chunk:
                    chunk_text = "\n".join(current_chunk).strip()
                    if chunk_text:
                        chunks.append(
                            {
                                "content": chunk_text,
                                "start": current_start,
                                "end": char_pos,
                                "headings": current_headings.copy(),
                                "type": ChunkType.TEXT,
                            }
                        )
                    current_chunk = []

                # Find closing fence
                lang = line[3:].strip().split()[0] if line[3:].strip() else None
                code_lines = [line]
                code_start = char_pos
                char_pos += line_len

                for next_line in lines[lines.index(line) + 1 :]:
                    code_lines.append(next_line)
                    char_pos += len(next_line) + 1
                    if next_line.startswith("```"):
                        break

                # Add code chunk
                code_content = "\n".join(code_lines)
                chunks.append(
                    {
                        "content": code_content,
                        "start": code_start,
                        "end": char_pos,
                        "headings": current_headings.copy(),
                        "type": ChunkType.CODE,
                        "language": lang,
                    }
                )
                current_start = char_pos
                continue

            # Check for paragraph break
            elif line.strip() == "" and current_chunk:
                chunk_text = "\n".join(current_chunk).strip()
                if len(chunk_text) > self.max_chunk_chars // 2:
                    # Large enough to be its own chunk
                    chunks.append(
                        {
                            "content": chunk_text,
                            "start": current_start,
                            "end": char_pos,
                            "headings": current_headings.copy(),
                            "type": ChunkType.TEXT,
                        }
                    )
                    current_chunk = []
                    current_start = char_pos + line_len
                else:
                    current_chunk.append(line)

            else:
                current_chunk.append(line)

                # Check if current chunk is too large
                chunk_text = "\n".join(current_chunk)
                if len(chunk_text) > self.max_chunk_chars:
                    chunks.append(
                        {
                            "content": chunk_text.strip(),
                            "start": current_start,
                            "end": char_pos + line_len,
                            "headings": current_headings.copy(),
                            "type": ChunkType.TEXT,
                        }
                    )
                    current_chunk = []
                    current_start = char_pos + line_len

            char_pos += line_len

        # Don't forget the last chunk
        if current_chunk:
            chunk_text = "\n".join(current_chunk).strip()
            if chunk_text:
                chunks.append(
                    {
                        "content": chunk_text,
                        "start": current_start,
                        "end": char_pos,
                        "headings": current_headings.copy(),
                        "type": ChunkType.TEXT,
                    }
                )

        # Merge small adjacent text chunks
        return self._merge_small_chunks(chunks)

    def _chunk_sliding_window(self, content: str) -> list[dict]:
        """Chunk using sliding window with overlap.

        Simple but effective for maintaining context across chunk boundaries.
        """
        chunks = []
        start = 0
        step = self.max_chunk_chars - self.overlap_chars

        # Ensure step is at least 1 to prevent infinite loops
        if step <= 0:
            step = max(1, self.max_chunk_chars // 2)

        while start < len(content):
            end = min(start + self.max_chunk_chars, len(content))

            # Try to break at word boundary
            if end < len(content) and len(content) > 0:
                # Look for space near the end (start from end-1 since end might be beyond last char)
                search_start = min(end - 1, len(content) - 1)
                search_end = max(start + step, end - 100, 0)
                # Ensure we have a valid range
                if search_start >= 0 and search_start > search_end:
                    for i in range(search_start, search_end, -1):
                        if content[i] == " ":
                            end = i
                            break

            chunk_content = content[start:end].strip()
            if chunk_content:
                chunks.append(
                    {
                        "content": chunk_content,
                        "start": start,
                        "end": end,
                        "type": ChunkType.TEXT,
                        "headings": [],
                    }
                )

            start += step

        return chunks

    def _chunk_code_aware(self, content: str) -> list[dict]:
        """Chunk with special handling for code blocks.

        Keeps code blocks intact when possible, uses semantic chunking for prose.
        """
        # Split content into code and non-code segments
        code_pattern = r"(```[\s\S]*?```)"
        segments = re.split(code_pattern, content)

        chunks = []
        char_pos = 0

        for segment in segments:
            if segment.startswith("```"):
                # Code block - keep intact if not too large
                lang_match = re.match(r"```(\w+)?", segment)
                lang = lang_match.group(1) if lang_match else None

                if len(segment) <= self.max_chunk_chars * 2:
                    # Keep as single chunk
                    chunks.append(
                        {
                            "content": segment,
                            "start": char_pos,
                            "end": char_pos + len(segment),
                            "type": ChunkType.CODE,
                            "language": lang,
                            "headings": [],
                        }
                    )
                else:
                    # Split large code blocks by lines
                    lines = segment.split("\n")
                    current_lines = []
                    chunk_start = char_pos

                    for line in lines:
                        current_lines.append(line)
                        current_content = "\n".join(current_lines)

                        if len(current_content) > self.max_chunk_chars:
                            chunks.append(
                                {
                                    "content": current_content,
                                    "start": chunk_start,
                                    "end": char_pos + len(current_content),
                                    "type": ChunkType.CODE,
                                    "language": lang,
                                    "headings": [],
                                }
                            )
                            current_lines = []
                            chunk_start = char_pos + len(current_content) + 1

                    if current_lines:
                        chunks.append(
                            {
                                "content": "\n".join(current_lines),
                                "start": chunk_start,
                                "end": char_pos + len(segment),
                                "type": ChunkType.CODE,
                                "language": lang,
                                "headings": [],
                            }
                        )
            # Non-code content - use semantic chunking
            elif segment.strip():
                text_chunks = self._chunk_semantic(segment)
                for tc in text_chunks:
                    tc["start"] += char_pos
                    tc["end"] += char_pos
                    chunks.append(tc)

            char_pos += len(segment)

        return chunks

    def _merge_small_chunks(
        self,
        chunks: list[dict],
        min_size: int | None = None,
    ) -> list[dict]:
        """Merge adjacent small chunks of the same type."""
        if not chunks:
            return chunks

        if min_size is None:
            min_size = self.max_chunk_chars // 4

        merged = []
        current = chunks[0].copy()

        for chunk in chunks[1:]:
            # Can merge if same type and combined size is reasonable
            can_merge = (
                chunk["type"] == current["type"]
                and len(current["content"]) < min_size
                and len(current["content"]) + len(chunk["content"]) <= self.max_chunk_chars
            )

            if can_merge:
                current["content"] += "\n\n" + chunk["content"]
                current["end"] = chunk["end"]
            else:
                if current["content"].strip():
                    merged.append(current)
                current = chunk.copy()

        if current["content"].strip():
            merged.append(current)

        return merged

    def _build_document_context(self, document: CrawledDocument) -> str:
        """Build document-level context for chunk prefixes.

        Used for Anthropic's contextual retrieval technique.
        """
        parts = []

        if document.title:
            parts.append(f"Document: {document.title}")

        if document.section_path:
            parts.append(f"Section: {' > '.join(document.section_path)}")

        # Add source info if available
        parts.append(f"Source: {document.url}")

        return " | ".join(parts)

    def _generate_chunk_context(self, doc_context: str, chunk: dict) -> str:
        """Generate contextual prefix for a chunk.

        Implements Anthropic's technique: prepend situating context.
        """
        parts = [doc_context]

        # Add heading context
        if chunk.get("headings"):
            parts.append("Section: " + " > ".join(chunk["headings"]))

        # Add type context
        if chunk.get("type") == ChunkType.CODE:
            lang = chunk.get("language", "code")
            parts.append(f"Content type: {lang} code example")
        elif chunk.get("type") == ChunkType.HEADING:
            parts.append("Content type: section heading")

        return " | ".join(parts)


def chunk_document(
    document: CrawledDocument,
    *,
    strategy: ChunkStrategy = ChunkStrategy.SEMANTIC,
    max_tokens: int | None = None,
    include_context: bool = True,
) -> list[Chunk]:
    """Convenience function to chunk a document.

    Args:
        document: Document to chunk
        strategy: Chunking strategy
        max_tokens: Maximum tokens per chunk
        include_context: Whether to include contextual prefixes

    Returns:
        List of chunks
    """
    chunker = DocumentChunker(
        max_chunk_tokens=max_tokens,
        include_context=include_context,
    )
    return chunker.chunk_document(document, strategy=strategy)
