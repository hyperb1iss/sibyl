"""Markdown parser for wisdom docs and other knowledge sources."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import mistune
import yaml


@dataclass
class CodeBlock:
    """A code block extracted from markdown."""

    language: str
    content: str
    line_number: int


@dataclass
class Section:
    """A markdown section with header and content."""

    level: int  # 1 = H1, 2 = H2, etc.
    title: str
    content: str
    line_number: int
    code_blocks: list[CodeBlock] = field(default_factory=list)
    subsections: list["Section"] = field(default_factory=list)


@dataclass
class ParsedDocument:
    """A fully parsed markdown document."""

    file_path: Path
    frontmatter: dict[str, object]
    title: str  # From H1 or frontmatter
    sections: list[Section]
    raw_content: str
    word_count: int

    @property
    def all_sections_flat(self) -> list[Section]:
        """Get all sections flattened (including nested subsections)."""
        result: list[Section] = []

        def collect(sections: list[Section]) -> None:
            for section in sections:
                result.append(section)
                collect(section.subsections)

        collect(self.sections)
        return result


class MarkdownParser:
    """Parser for markdown files with frontmatter support."""

    # Pattern to extract YAML frontmatter
    FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

    # Pattern to match headers
    HEADER_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    # Pattern to match fenced code blocks
    CODE_BLOCK_PATTERN = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

    def __init__(self) -> None:
        """Initialize the parser."""
        self._mistune = mistune.create_markdown()

    def parse_file(self, file_path: Path) -> ParsedDocument:
        """Parse a markdown file.

        Args:
            file_path: Path to the markdown file.

        Returns:
            ParsedDocument with extracted structure.
        """
        content = file_path.read_text(encoding="utf-8")
        return self.parse_content(content, file_path)

    def parse_content(self, content: str, file_path: Path | None = None) -> ParsedDocument:
        """Parse markdown content.

        Args:
            content: Raw markdown content.
            file_path: Optional source file path.

        Returns:
            ParsedDocument with extracted structure.
        """
        # Extract frontmatter
        frontmatter, body = self._extract_frontmatter(content)

        # Extract sections
        sections = self._extract_sections(body)

        # Determine title
        title = self._determine_title(frontmatter, sections, file_path)

        # Count words (excluding code blocks)
        word_count = self._count_words(body)

        return ParsedDocument(
            file_path=file_path or Path("unknown.md"),
            frontmatter=frontmatter,
            title=title,
            sections=sections,
            raw_content=content,
            word_count=word_count,
        )

    def _extract_frontmatter(self, content: str) -> tuple[dict[str, object], str]:
        """Extract YAML frontmatter from content.

        Args:
            content: Raw markdown content.

        Returns:
            Tuple of (frontmatter dict, remaining content).
        """
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            return {}, content

        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            frontmatter = {}

        body = content[match.end() :]
        return frontmatter, body

    def _extract_sections(self, content: str) -> list[Section]:
        """Extract hierarchical sections from markdown content.

        Args:
            content: Markdown content without frontmatter.

        Returns:
            List of top-level sections with nested subsections.
        """
        lines = content.split("\n")
        sections: list[Section] = []
        current_section: Section | None = None
        section_stack: list[Section] = []
        current_content_lines: list[str] = []
        current_code_blocks: list[CodeBlock] = []

        in_code_block = False
        code_block_lang = ""
        code_block_lines: list[str] = []
        code_block_start = 0

        for line_num, line in enumerate(lines, 1):
            # Track code block state
            if line.startswith("```"):
                if not in_code_block:
                    in_code_block = True
                    code_block_lang = line[3:].strip()
                    code_block_lines = []
                    code_block_start = line_num
                else:
                    in_code_block = False
                    current_code_blocks.append(
                        CodeBlock(
                            language=code_block_lang,
                            content="\n".join(code_block_lines),
                            line_number=code_block_start,
                        )
                    )
                current_content_lines.append(line)
                continue

            if in_code_block:
                code_block_lines.append(line)
                current_content_lines.append(line)
                continue

            # Check for header
            header_match = self.HEADER_PATTERN.match(line)
            if header_match:
                # Save current section content
                if current_section is not None:
                    current_section.content = "\n".join(current_content_lines).strip()
                    current_section.code_blocks = current_code_blocks

                level = len(header_match.group(1))
                title = header_match.group(2).strip()

                new_section = Section(
                    level=level,
                    title=title,
                    content="",
                    line_number=line_num,
                    code_blocks=[],
                    subsections=[],
                )

                # Find parent section
                while section_stack and section_stack[-1].level >= level:
                    section_stack.pop()

                if section_stack:
                    section_stack[-1].subsections.append(new_section)
                else:
                    sections.append(new_section)

                section_stack.append(new_section)
                current_section = new_section
                current_content_lines = []
                current_code_blocks = []
            else:
                current_content_lines.append(line)

        # Save final section content
        if current_section is not None:
            current_section.content = "\n".join(current_content_lines).strip()
            current_section.code_blocks = current_code_blocks

        return sections

    def _determine_title(
        self,
        frontmatter: dict[str, object],
        sections: list[Section],
        file_path: Path | None,
    ) -> str:
        """Determine document title from various sources.

        Args:
            frontmatter: Parsed YAML frontmatter.
            sections: Extracted sections.
            file_path: Source file path.

        Returns:
            Best available title.
        """
        # Priority 1: Frontmatter title
        if "title" in frontmatter:
            return str(frontmatter["title"])

        # Priority 2: First H1
        for section in sections:
            if section.level == 1:
                return section.title

        # Priority 3: File name
        if file_path:
            return file_path.stem.replace("-", " ").replace("_", " ").title()

        return "Untitled"

    def _count_words(self, content: str) -> int:
        """Count words in content, excluding code blocks.

        Args:
            content: Markdown content.

        Returns:
            Word count.
        """
        # Remove code blocks
        text = self.CODE_BLOCK_PATTERN.sub("", content)
        # Count words
        words = text.split()
        return len(words)


def parse_directory(directory: Path, pattern: str = "**/*.md") -> list[ParsedDocument]:
    """Parse all markdown files in a directory.

    Args:
        directory: Root directory to search.
        pattern: Glob pattern for files.

    Returns:
        List of parsed documents.
    """
    parser = MarkdownParser()
    documents = []

    for file_path in directory.glob(pattern):
        if file_path.is_file():
            try:
                doc = parser.parse_file(file_path)
                documents.append(doc)
            except Exception as e:
                # Log but continue on parse errors
                import structlog

                structlog.get_logger().warning(
                    "Failed to parse markdown file",
                    file=str(file_path),
                    error=str(e),
                )

    return documents
