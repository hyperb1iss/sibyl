"""Lookup tools for the Conventions MCP Server.

Provides direct access to specific entities like rules, guides, and templates.
"""

from dataclasses import dataclass, field

import structlog

from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.models.entities import EntityType

log = structlog.get_logger()


@dataclass
class RuleResult:
    """A sacred rule with full details."""

    id: str
    name: str
    description: str
    severity: str
    category: str | None = None
    enforcement: str | None = None
    exceptions: list[str] = field(default_factory=list)
    examples: dict[str, str] | None = None  # wrong vs right


@dataclass
class LanguageGuide:
    """Complete conventions guide for a programming language."""

    language: str
    tooling: str
    patterns: str
    style: str
    testing: str
    full_content: str


@dataclass
class TemplateResult:
    """A template with full content and metadata."""

    id: str
    name: str
    template_type: str
    language: str | None
    description: str
    content: str
    variables: list[str]
    file_path: str | None = None


async def get_sacred_rules(
    category: str | None = None,
    language: str | None = None,
    severity: str | None = None,
) -> list[RuleResult]:
    """Get all sacred rules/invariants.

    Args:
        category: Filter by category (development, type_safety, database, etc.).
        language: Filter by programming language.
        severity: Filter by severity (error, warning, info).

    Returns:
        List of sacred rules matching the filters.
    """
    log.info("Getting sacred rules", category=category, language=language, severity=severity)

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Get all rule entities
        rules = await entity_manager.list_by_type(EntityType.RULE, limit=100)

        results = []
        for rule in rules:
            # Apply filters
            if category:
                rule_category = getattr(rule, "category", "") or ""
                if category.lower() not in rule_category.lower():
                    continue

            if severity:
                rule_severity = getattr(rule, "severity", "error")
                if severity.lower() != rule_severity.lower():
                    continue

            if language:
                rule_languages = getattr(rule, "languages", []) or []
                if language.lower() not in [l.lower() for l in rule_languages]:
                    continue

            results.append(
                RuleResult(
                    id=rule.id,
                    name=rule.name,
                    description=rule.description,
                    severity=getattr(rule, "severity", "error"),
                    category=getattr(rule, "category", None),
                    enforcement=getattr(rule, "enforcement", None),
                    exceptions=getattr(rule, "exceptions", []),
                )
            )

        return results

    except Exception as e:
        log.warning("Failed to get sacred rules", error=str(e))
        return []


async def get_language_guide(
    language: str,
    section: str | None = None,
) -> LanguageGuide | None:
    """Get the complete conventions guide for a programming language.

    Args:
        language: Programming language (python, typescript, rust, swift, etc.).
        section: Specific section to return (tooling, patterns, style, testing, all).

    Returns:
        LanguageGuide with the requested information, or None if not found.
    """
    log.info("Getting language guide", language=language, section=section)

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Search for the language entity
        results = await entity_manager.search(
            query=f"{language} conventions guide",
            entity_types=[EntityType.LANGUAGE, EntityType.EPISODE],
            limit=5,
        )

        if not results:
            log.warning("Language guide not found", language=language)
            return None

        # Find the most relevant result
        best_match = None
        for entity, score in results:
            if entity.name.lower() == language.lower():
                best_match = entity
                break
            if language.lower() in entity.name.lower() and not best_match:
                best_match = entity

        if not best_match:
            best_match, _ = results[0]

        # Build the guide from the entity
        content = best_match.content or best_match.description or ""

        # Try to extract sections (this is a simplified version)
        # In a full implementation, this would parse structured content
        guide = LanguageGuide(
            language=language,
            tooling=_extract_section(content, "tooling", "tools"),
            patterns=_extract_section(content, "patterns", "pattern"),
            style=_extract_section(content, "style", "formatting"),
            testing=_extract_section(content, "testing", "test"),
            full_content=content,
        )

        # If a specific section was requested, only return that
        if section and section != "all":
            section_content = getattr(guide, section, None)
            if section_content:
                guide.full_content = section_content

        return guide

    except Exception as e:
        log.warning("Failed to get language guide", error=str(e), language=language)
        return None


def _extract_section(content: str, *keywords: str) -> str:
    """Extract a section from content based on keywords.

    Simple heuristic: find lines containing keywords and return surrounding content.
    """
    lines = content.split("\n")
    section_lines = []
    in_section = False

    for i, line in enumerate(lines):
        line_lower = line.lower()

        # Check if this is a section header with our keywords
        if any(kw in line_lower for kw in keywords) and (
            line.startswith("#") or line.startswith("##")
        ):
            in_section = True
            section_lines = [line]
            continue

        # If we're in the section, collect lines until next header
        if in_section:
            if line.startswith("#") and not any(kw in line.lower() for kw in keywords):
                break
            section_lines.append(line)

    return "\n".join(section_lines).strip() if section_lines else ""


async def get_template(
    name: str,
    template_type: str | None = None,
    language: str | None = None,
) -> TemplateResult | None:
    """Get a specific template by name.

    Args:
        name: Template name or identifier.
        template_type: Optional type filter (code, config, project).
        language: Optional language filter.

    Returns:
        TemplateResult with full content, or None if not found.
    """
    log.info("Getting template", name=name, template_type=template_type)

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Search for the template
        results = await entity_manager.search(
            query=name,
            entity_types=[EntityType.TEMPLATE, EntityType.CONFIG_FILE],
            limit=10,
        )

        if not results:
            log.warning("Template not found", name=name)
            return None

        # Find the best match
        best_match = None
        for entity, score in results:
            # Exact name match takes priority
            if entity.name.lower() == name.lower():
                best_match = entity
                break

            # Apply type filter
            if template_type:
                entity_type = getattr(entity, "template_type", "") or ""
                if template_type.lower() != entity_type.lower():
                    continue

            # Apply language filter
            if language:
                entity_lang = getattr(entity, "language", "") or ""
                if language.lower() not in entity_lang.lower():
                    continue

            if not best_match:
                best_match = entity

        if not best_match:
            return None

        return TemplateResult(
            id=best_match.id,
            name=best_match.name,
            template_type=getattr(best_match, "template_type", "unknown"),
            language=getattr(best_match, "language", None),
            description=best_match.description,
            content=best_match.content or "",
            variables=getattr(best_match, "variables", []),
            file_path=best_match.source_file,
        )

    except Exception as e:
        log.warning("Failed to get template", error=str(e), name=name)
        return None
