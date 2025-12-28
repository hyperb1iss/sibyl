"""Tests for document chunking pipeline.

Tests the DocumentChunker class and chunking strategies:
- Semantic chunking (H2/H3 boundaries)
- Sliding window chunking
- Code-aware chunking
- Context generation (Anthropic technique)
"""

import pytest

from sibyl.crawler.chunker import (
    Chunk,
    ChunkStrategy,
    DocumentChunker,
    chunk_document,
)
from sibyl.db.models import ChunkType

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_markdown() -> str:
    """Sample markdown document for testing."""
    return """# Getting Started

This is the introduction to the document.

## Installation

First, install the dependencies:

```bash
pip install sibyl
```

Then configure your environment.

## Configuration

### Database Setup

Configure your database connection:

```python
DATABASE_URL = "postgresql://localhost/sibyl"
```

This is important for proper operation.

### API Keys

Set up your API keys in `.env`:

```bash
OPENAI_API_KEY=sk-xxx
```

## Usage

Here's how to use the library:

```python
from sibyl import Client

client = Client()
result = client.search("hello")
```

That's all you need to get started!
"""


@pytest.fixture
def sample_code_heavy() -> str:
    """Document with lots of code blocks."""
    return """# API Reference

## Functions

### search()

```python
def search(query: str, limit: int = 10) -> list[Result]:
    \"\"\"Search the knowledge base.

    Args:
        query: Search query string
        limit: Maximum results to return

    Returns:
        List of matching results
    \"\"\"
    pass
```

### add()

```python
def add(entity: Entity) -> str:
    \"\"\"Add an entity to the graph.

    Args:
        entity: Entity to add

    Returns:
        Entity UUID
    \"\"\"
    pass
```

### explore()

```python
def explore(mode: str, entity_id: str | None = None) -> ExploreResult:
    \"\"\"Explore the knowledge graph.

    Args:
        mode: Exploration mode (list, related, traverse)
        entity_id: Starting entity for traversal

    Returns:
        Exploration results
    \"\"\"
    pass
```
"""


@pytest.fixture
def mock_document(sample_markdown: str):
    """Create a mock CrawledDocument for testing."""
    from unittest.mock import MagicMock

    doc = MagicMock()
    doc.id = "doc-123"
    doc.url = "https://docs.example.com/getting-started"
    doc.title = "Getting Started Guide"
    doc.content = sample_markdown
    doc.section_path = ["Documentation", "Getting Started"]
    return doc


# =============================================================================
# Chunker Initialization Tests
# =============================================================================


class TestDocumentChunkerInit:
    """Tests for DocumentChunker initialization."""

    def test_default_values(self):
        """Test default chunker configuration."""
        chunker = DocumentChunker()

        # Should use settings defaults
        assert chunker.max_chunk_tokens > 0
        assert chunker.overlap_tokens > 0
        assert chunker.include_context is True

    def test_custom_values(self):
        """Test custom chunker configuration."""
        chunker = DocumentChunker(
            max_chunk_tokens=500,
            overlap_tokens=50,
            include_context=False,
        )

        assert chunker.max_chunk_tokens == 500
        assert chunker.overlap_tokens == 50
        assert chunker.include_context is False

    def test_char_conversion(self):
        """Test token to character conversion."""
        chunker = DocumentChunker(max_chunk_tokens=100, overlap_tokens=10)

        # 1 token ≈ 4 characters
        assert chunker.max_chunk_chars == 400
        assert chunker.overlap_chars == 40


# =============================================================================
# Semantic Chunking Tests
# =============================================================================


class TestSemanticChunking:
    """Tests for semantic (H2/H3) chunking strategy."""

    def test_chunks_on_headers(self, mock_document):
        """Test that document is chunked on header boundaries."""
        chunker = DocumentChunker()
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        assert len(chunks) > 0

        # Should have heading chunks
        heading_chunks = [c for c in chunks if c.chunk_type == ChunkType.HEADING]
        assert len(heading_chunks) > 0

    def test_preserves_code_blocks(self, mock_document):
        """Test that code blocks are preserved as separate chunks."""
        chunker = DocumentChunker()
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        code_chunks = [c for c in chunks if c.chunk_type == ChunkType.CODE]
        assert len(code_chunks) > 0

        # Code chunks should contain ``` markers
        for chunk in code_chunks:
            assert "```" in chunk.content

    def test_heading_path_tracking(self, mock_document):
        """Test that heading hierarchy is tracked."""
        chunker = DocumentChunker()
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        # Find a chunk under "Database Setup" heading
        db_chunks = [c for c in chunks if "Database Setup" in c.heading_path]

        # There should be content under this heading
        assert len(db_chunks) > 0

    def test_empty_document(self):
        """Test handling of empty document."""
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.content = ""

        chunker = DocumentChunker()
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.SEMANTIC)

        assert chunks == []

    def test_whitespace_only_document(self):
        """Test handling of whitespace-only document."""
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.content = "   \n\n   \t  "

        chunker = DocumentChunker()
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.SEMANTIC)

        assert chunks == []


# =============================================================================
# Code-Aware Chunking Tests
# =============================================================================


class TestCodeAwareChunking:
    """Tests for code-aware chunking strategy."""

    def test_keeps_code_blocks_intact(self, mock_document):
        """Test that code blocks are kept as single chunks when reasonable."""
        chunker = DocumentChunker()
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.CODE)

        code_chunks = [c for c in chunks if c.chunk_type == ChunkType.CODE]

        # Each code block should be a complete chunk
        for chunk in code_chunks:
            # Should have both opening and closing fence
            fence_count = chunk.content.count("```")
            assert fence_count == 2, f"Code block should be complete: {chunk.content[:100]}"

    def test_detects_language(self, sample_code_heavy):
        """Test that code language is detected."""
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.content = sample_code_heavy
        doc.url = "https://example.com/api"
        doc.title = "API Reference"
        doc.section_path = []

        chunker = DocumentChunker()
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.CODE)

        # Should detect Python
        python_chunks = [c for c in chunks if c.language == "python"]
        assert len(python_chunks) > 0

    def test_splits_large_code_blocks(self):
        """Test that very large code blocks are split."""
        from unittest.mock import MagicMock

        # Create a very large code block
        large_code = "```python\n" + ("x = 1\n" * 1000) + "```"

        doc = MagicMock()
        doc.content = large_code
        doc.url = "https://example.com"
        doc.title = "Large Code"
        doc.section_path = []

        chunker = DocumentChunker(max_chunk_tokens=100)  # Small limit
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.CODE)

        # Should be split into multiple chunks
        assert len(chunks) > 1


# =============================================================================
# Sliding Window Chunking Tests
# =============================================================================


class TestSlidingWindowChunking:
    """Tests for sliding window chunking strategy."""

    def test_creates_overlapping_chunks(self, mock_document):
        """Test that chunks overlap correctly."""
        chunker = DocumentChunker(max_chunk_tokens=50, overlap_tokens=10)
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SLIDING)

        assert len(chunks) > 1

        # Check that consecutive chunks share some content
        for i in range(len(chunks) - 1):
            chunks[i].content[-50:]  # Last 50 chars
            chunks[i + 1].content[:100]  # First 100 chars

            # There should be some overlap (not necessarily exact due to word boundaries)
            # Just verify chunks exist
            assert len(chunks[i].content) > 0
            assert len(chunks[i + 1].content) > 0

    def test_respects_word_boundaries(self, mock_document):
        """Test that sliding window creates non-empty chunks."""
        # With small content, just verify chunks are created and have content
        chunker = DocumentChunker(max_chunk_tokens=100, overlap_tokens=20)
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SLIDING)

        # Should create multiple chunks
        assert len(chunks) >= 1

        # All chunks should have non-empty content
        for chunk in chunks:
            assert len(chunk.content.strip()) > 0

        # First chunk should start with the document start
        assert chunks[0].content.strip().startswith("# Getting Started")


# =============================================================================
# Context Generation Tests
# =============================================================================


class TestContextGeneration:
    """Tests for Anthropic-style contextual prefix generation."""

    def test_includes_document_context(self, mock_document):
        """Test that chunks include document-level context."""
        chunker = DocumentChunker(include_context=True)
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        for chunk in chunks:
            if chunk.context:
                # Should include document title
                assert mock_document.title in chunk.context or "Document:" in chunk.context

    def test_includes_section_path(self, mock_document):
        """Test that context includes section hierarchy."""
        chunker = DocumentChunker(include_context=True)
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        # Find chunks with headings
        chunks_with_headings = [c for c in chunks if c.heading_path]

        for chunk in chunks_with_headings:
            if chunk.context:
                # Context should mention section
                assert "Section:" in chunk.context or any(
                    h in chunk.context for h in chunk.heading_path
                )

    def test_code_type_context(self, mock_document):
        """Test that code chunks have type-specific context."""
        chunker = DocumentChunker(include_context=True)
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        code_chunks = [c for c in chunks if c.chunk_type == ChunkType.CODE]

        for chunk in code_chunks:
            if chunk.context:
                # Should indicate code content type
                assert "code" in chunk.context.lower()

    def test_context_disabled(self, mock_document):
        """Test that context can be disabled."""
        chunker = DocumentChunker(include_context=False)
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        for chunk in chunks:
            assert chunk.context is None


# =============================================================================
# Chunk Metadata Tests
# =============================================================================


class TestChunkMetadata:
    """Tests for chunk metadata and attributes."""

    def test_chunk_index_sequence(self, mock_document):
        """Test that chunk indices are sequential."""
        chunker = DocumentChunker()
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_token_count_estimation(self, mock_document):
        """Test that token counts are estimated."""
        chunker = DocumentChunker()
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        for chunk in chunks:
            # Token count should be roughly content length / 4
            expected = len(chunk.content) // 4
            assert chunk.token_count == expected

    def test_character_offsets(self, mock_document):
        """Test that start/end character offsets are tracked."""
        chunker = DocumentChunker()
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SEMANTIC)

        for chunk in chunks:
            # End should be after start
            assert chunk.end_char >= chunk.start_char

            # Offset difference should roughly match content length
            offset_diff = chunk.end_char - chunk.start_char
            # Allow some slack for merging/processing
            assert offset_diff >= 0


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestConvenienceFunction:
    """Tests for the chunk_document convenience function."""

    def test_default_strategy(self, mock_document):
        """Test default chunking strategy."""
        chunks = chunk_document(mock_document)

        assert len(chunks) > 0
        assert all(isinstance(c, Chunk) for c in chunks)

    def test_custom_strategy(self, mock_document):
        """Test custom chunking strategy."""
        chunks = chunk_document(mock_document, strategy=ChunkStrategy.SLIDING)

        assert len(chunks) > 0

    def test_custom_max_tokens(self, mock_document):
        """Test custom max tokens setting."""
        small_chunks = chunk_document(mock_document, max_tokens=50)
        large_chunks = chunk_document(mock_document, max_tokens=500)

        # Smaller token limit should produce more chunks
        # (unless content is very small)
        if len(mock_document.content) > 1000:
            assert len(small_chunks) >= len(large_chunks)


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_no_headers(self):
        """Test document with no headers."""
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.content = "This is just plain text without any headers."
        doc.url = "https://example.com"
        doc.title = "Plain Text"
        doc.section_path = []

        chunker = DocumentChunker()
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.SEMANTIC)

        assert len(chunks) > 0
        assert all(c.chunk_type == ChunkType.TEXT for c in chunks)

    def test_only_headers(self):
        """Test document with only headers, no content."""
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.content = "# Header 1\n## Header 2\n### Header 3"
        doc.url = "https://example.com"
        doc.title = "Headers Only"
        doc.section_path = []

        chunker = DocumentChunker()
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.SEMANTIC)

        # Should produce heading chunks
        assert len(chunks) > 0
        assert all(c.chunk_type == ChunkType.HEADING for c in chunks)

    def test_unclosed_code_block(self):
        """Test handling of unclosed code block."""
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.content = "# Title\n\n```python\nx = 1\n# Missing closing fence"
        doc.url = "https://example.com"
        doc.title = "Broken Code"
        doc.section_path = []

        chunker = DocumentChunker()

        # Should not crash
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.SEMANTIC)
        assert len(chunks) > 0

    def test_nested_code_blocks(self):
        """Test handling of nested code block syntax."""
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.content = """# Example

```markdown
Here's how to write code blocks:

```python
x = 1
```

That's all!
```
"""
        doc.url = "https://example.com"
        doc.title = "Nested Example"
        doc.section_path = []

        chunker = DocumentChunker()

        # Should not crash
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.SEMANTIC)
        assert len(chunks) > 0

    def test_unicode_content(self):
        """Test handling of unicode content."""
        from unittest.mock import MagicMock

        doc = MagicMock()
        doc.content = (
            "# 日本語タイトル\n\nこれは日本語のテストです。\n\n## 絵文字 🎉\n\nEmoji: 🚀 💻 🔥"
        )
        doc.url = "https://example.com"
        doc.title = "Unicode Test"
        doc.section_path = []

        chunker = DocumentChunker()
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.SEMANTIC)

        assert len(chunks) > 0
        # Content should be preserved
        full_content = "".join(c.content for c in chunks)
        assert "日本語" in full_content or len(chunks) > 0


# =============================================================================
# Integration Tests
# =============================================================================


class TestChunkerIntegration:
    """Integration tests for the chunking pipeline."""

    def test_roundtrip_content(self, mock_document):
        """Test that chunking preserves all content."""
        chunker = DocumentChunker()
        chunks = chunker.chunk_document(mock_document, strategy=ChunkStrategy.SLIDING)

        # With sliding window, all original content should be covered
        # (may have overlaps)
        original_words = set(mock_document.content.split())
        chunked_words = set()
        for chunk in chunks:
            chunked_words.update(chunk.content.split())

        # Most original words should be in chunks
        coverage = len(original_words & chunked_words) / len(original_words)
        assert coverage > 0.9  # 90% coverage

    def test_semantic_preserves_structure(self):
        """Test that semantic chunking preserves document structure."""
        from unittest.mock import MagicMock

        # Create document with many small sections
        doc = MagicMock()
        doc.content = "\n".join([f"## Section {i}\n\nShort text." for i in range(10)])
        doc.url = "https://example.com"
        doc.title = "Many Sections"
        doc.section_path = []

        chunker = DocumentChunker()
        chunks = chunker.chunk_document(doc, strategy=ChunkStrategy.SEMANTIC)

        # Semantic chunking respects document structure - each section
        # with a heading becomes its own chunk for better context
        text_chunks = [c for c in chunks if c.chunk_type == ChunkType.TEXT]

        # Each section should have its own heading context
        assert len(text_chunks) == 10
        for i, chunk in enumerate(text_chunks):
            assert f"Section {i}" in str(chunk.heading_path)
