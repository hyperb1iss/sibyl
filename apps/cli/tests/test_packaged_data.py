"""Tests that packaged CLI assets stay in sync with repo-local sources."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


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
