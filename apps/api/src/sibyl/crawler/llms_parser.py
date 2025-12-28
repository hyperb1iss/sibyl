"""Parser for llms-full.txt files.

Splits llms-full.txt content by H1 headers (# Title) into separate sections,
each with a synthetic URL anchor for indexing as separate documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class LLMsSection:
    """A section parsed from an llms-full.txt file."""

    title: str  # H1 text without "# " prefix
    section_order: int  # Position in document (0-based)
    content: str  # Full section content including H1
    url: str  # Synthetic URL with anchor
    word_count: int


def create_slug(heading: str) -> str:
    """Generate URL slug from heading text.

    Args:
        heading: Heading text like "# Core Concepts"

    Returns:
        Slug like "core-concepts"

    Examples:
        "# Core Concepts" -> "core-concepts"
        "# API Reference" -> "api-reference"
        "# Getting Started!" -> "getting-started"
    """
    # Remove "# " prefix if present
    slug_text = heading.lstrip("#").strip()

    # Convert to lowercase and replace spaces with hyphens
    slug = slug_text.lower().replace(" ", "-")

    # Remove special characters (keep only alphanumeric and hyphens)
    slug = re.sub(r"[^a-z0-9-]", "", slug)

    # Remove consecutive hyphens and trim
    return re.sub(r"-+", "-", slug).strip("-")


def create_section_url(base_url: str, heading: str, order: int) -> str:
    """Generate synthetic URL with anchor for a section.

    Args:
        base_url: Base URL like "https://example.com/llms-full.txt"
        heading: H1 text like "# Core Concepts"
        order: Section position (0-based)

    Returns:
        URL like "https://example.com/llms-full.txt#section-0-core-concepts"
    """
    slug = create_slug(heading)
    return f"{base_url}#section-{order}-{slug}"


def parse_llms_full(content: str, base_url: str) -> list[LLMsSection]:
    """Split llms-full.txt content by H1 headers into sections.

    Each H1 (lines starting with "# " but not "##") marks a new section.
    Handles code blocks correctly (doesn't split on # inside ```).
    Combines small sections (<200 chars) together.

    Args:
        content: Full text content of llms-full.txt
        base_url: Base URL for generating synthetic anchors

    Returns:
        List of LLMsSection objects, one per H1 section

    Edge cases:
        - No H1 headers: Returns single section with entire content
        - Empty sections: Skipped
        - H1 inside code blocks: Ignored
    """
    lines = content.split("\n")

    # Pre-scan: mark lines inside code blocks
    inside_code_block: set[int] = set()
    in_block = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_block = not in_block
        if in_block:
            inside_code_block.add(i)

    # Parse sections
    sections: list[LLMsSection] = []
    current_h1: str | None = None
    current_content: list[str] = []
    section_order = 0

    for i, line in enumerate(lines):
        # Detect H1 (starts with "# " but not "##") outside code blocks
        is_h1 = line.startswith("# ") and not line.startswith("## ")

        if is_h1 and i not in inside_code_block:
            # Save previous section if exists
            if current_h1 is not None:
                section_text = "\n".join(current_content)
                if section_text.strip():
                    sections.append(
                        LLMsSection(
                            title=current_h1.lstrip("#").strip(),
                            section_order=section_order,
                            content=section_text,
                            url=create_section_url(base_url, current_h1, section_order),
                            word_count=len(section_text.split()),
                        )
                    )
                    section_order += 1

            # Start new section
            current_h1 = line
            current_content = [line]
        # Accumulate content if we've seen an H1
        elif current_h1 is not None:
            current_content.append(line)

    # Save last section
    if current_h1 is not None:
        section_text = "\n".join(current_content)
        if section_text.strip():
            sections.append(
                LLMsSection(
                    title=current_h1.lstrip("#").strip(),
                    section_order=section_order,
                    content=section_text,
                    url=create_section_url(base_url, current_h1, section_order),
                    word_count=len(section_text.split()),
                )
            )

    # Edge case: No H1 headers found
    if not sections and content.strip():
        sections.append(
            LLMsSection(
                title="Full Document",
                section_order=0,
                content=content,
                url=base_url,
                word_count=len(content.split()),
            )
        )

    # Fix sections split inside unclosed code blocks
    sections = _fix_unclosed_code_blocks(sections)

    # Combine consecutive small sections
    return _combine_small_sections(sections)


def _fix_unclosed_code_blocks(sections: list[LLMsSection]) -> list[LLMsSection]:
    """Merge sections that were incorrectly split inside code blocks."""
    if not sections:
        return sections

    fixed: list[LLMsSection] = []
    i = 0

    while i < len(sections):
        current = sections[i]

        # Count code fences
        fence_count = sum(
            1 for line in current.content.split("\n") if line.strip().startswith("```")
        )

        # If odd, we're inside unclosed block - merge with next
        while fence_count % 2 == 1 and i + 1 < len(sections):
            next_section = sections[i + 1]
            combined = current.content + "\n\n" + next_section.content
            current = LLMsSection(
                title=current.title,
                section_order=current.section_order,
                content=combined,
                url=current.url,
                word_count=len(combined.split()),
            )
            i += 1
            fence_count = sum(
                1 for line in current.content.split("\n") if line.strip().startswith("```")
            )

        fixed.append(current)
        i += 1

    return fixed


def _combine_small_sections(sections: list[LLMsSection], min_size: int = 200) -> list[LLMsSection]:
    """Combine consecutive small sections (<min_size chars) together."""
    if not sections:
        return sections

    combined: list[LLMsSection] = []
    i = 0

    while i < len(sections):
        current = sections[i]
        combined_content = current.content

        # Keep combining while small and more sections exist
        while len(combined_content) < min_size and i + 1 < len(sections):
            i += 1
            combined_content = combined_content + "\n\n" + sections[i].content

        combined.append(
            LLMsSection(
                title=current.title,
                section_order=current.section_order,
                content=combined_content,
                url=current.url,
                word_count=len(combined_content.split()),
            )
        )
        i += 1

    return combined
