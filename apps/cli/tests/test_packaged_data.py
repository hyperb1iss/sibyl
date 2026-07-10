"""Tests that packaged CLI assets stay in sync with repo-local sources."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STALE_AGENT_COMMAND = re.compile(
    r"(?m)^\s*sibyl (?:"
    r"add|blame|recall|search|"
    r"context(?:\s*(?:#.*)?$| (?:--quick|pack|list|show|create|use|update|delete|clear))|"
    r"memory-(?:audit|inspect|import-status|promote|share|space|review)|"
    r"context[^\n]*(?:--type|--graph-only|--docs-only)|"
    r"remember[^\n]*(?:--type|--category|--language|--title|--skip-conflicts)"
    r")(?:\s|$)"
)


def test_packaged_skill_and_hook_assets_match_repo_sources() -> None:
    """Embedded stub and hook assets should mirror repo-local copies exactly."""
    file_pairs = [
        (
            REPO_ROOT / "skills" / "sibyl" / "SKILL.md",
            REPO_ROOT
            / "apps"
            / "cli"
            / "src"
            / "sibyl_cli"
            / "data"
            / "skills"
            / "sibyl"
            / "SKILL.md",
        ),
        (
            REPO_ROOT / "hooks" / "session-start.py",
            REPO_ROOT
            / "apps"
            / "cli"
            / "src"
            / "sibyl_cli"
            / "data"
            / "hooks"
            / "session-start.py",
        ),
    ]

    mismatches = [
        f"{source.relative_to(REPO_ROOT)} != {packaged.relative_to(REPO_ROOT)}"
        for source, packaged in file_pairs
        if source.read_text() != packaged.read_text()
    ]

    assert mismatches == []


def test_cli_bundle_contains_versioned_skill_packs() -> None:
    """Full skill guidance should live in packaged markdown packs."""
    pack_dir = REPO_ROOT / "apps" / "cli" / "src" / "sibyl_cli" / "data" / "skill-packs"

    assert (pack_dir / "core.md").read_text().startswith("# Sibyl")
    contract = (pack_dir / "contract.md").read_text()
    assert contract.startswith("# Sibyl Agent Contract")
    assert "## Five verbs" in contract
    assert "## Five hard rules" in contract
    assert len(contract.split()) < 1400
    assert "Agent Rules (READ FIRST)" in (pack_dir / "core.md").read_text()
    assert "Sibyl CLI Workflows" in (pack_dir / "workflows.md").read_text()
    assert "Sibyl CLI Examples" in (pack_dir / "examples.md").read_text()
    assert "migration" in (pack_dir / "migration.md").read_text().lower()


def test_skill_packs_only_teach_converged_agent_commands() -> None:
    pack_dir = REPO_ROOT / "apps" / "cli" / "src" / "sibyl_cli" / "data" / "skill-packs"

    stale_examples = {
        path.name: STALE_AGENT_COMMAND.findall(path.read_text())
        for path in pack_dir.glob("*.md")
        if STALE_AGENT_COMMAND.search(path.read_text())
    }

    assert stale_examples == {}


def test_current_docs_only_teach_converged_cli_commands() -> None:
    excluded = {
        Path("docs/cli/add.md"),
        Path("docs/cli/recall.md"),
        Path("docs/cli/search.md"),
    }
    doc_paths = [REPO_ROOT / "README.md", REPO_ROOT / "apps" / "cli" / "README.md"]
    doc_paths.extend(
        path
        for path in (REPO_ROOT / "docs").rglob("*.md")
        if "_archive" not in path.parts
        and ".vitepress" not in path.parts
        and path.relative_to(REPO_ROOT) not in excluded
    )
    stale_examples = []
    for path in doc_paths:
        content = path.read_text()
        stale_examples.extend(
            f"{path.relative_to(REPO_ROOT)}:{content.count(chr(10), 0, match.start()) + 1}: "
            f"{match.group(0).strip()}"
            for match in STALE_AGENT_COMMAND.finditer(content)
        )

    assert stale_examples == []
