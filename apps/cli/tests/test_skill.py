"""Tests for the skill command."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sibyl_cli.main import app as main_app
from sibyl_cli.skill import app, canonical_skill_markdown, install_canonical_skill, skill_pack_markdown


def test_skill_command_prints_canonical_markdown() -> None:
    result = CliRunner().invoke(main_app, ["skill"])

    assert result.exit_code == 0
    assert result.stdout == canonical_skill_markdown()
    assert "version-matched workflow" in result.stdout
    assert "sibyl skill get core" in result.stdout
    assert "intent -> verb" in result.stdout


def test_skill_get_prints_versioned_pack() -> None:
    result = CliRunner().invoke(main_app, ["skill", "get", "core"])

    assert result.exit_code == 0
    assert result.stdout == skill_pack_markdown("core")
    assert "Agent Rules (READ FIRST)" in result.stdout


def test_skill_list_shows_available_packs() -> None:
    result = CliRunner().invoke(main_app, ["skill", "list"])

    assert result.exit_code == 0
    assert "core" in result.stdout
    assert "workflows" in result.stdout
    assert "examples" in result.stdout


def test_skill_get_rejects_unknown_pack() -> None:
    result = CliRunner().invoke(main_app, ["skill", "get", "missing"])

    assert result.exit_code == 1
    assert "Unknown skill pack: missing" in result.stdout


def test_skill_install_copies_to_roots(tmp_path: Path) -> None:
    claude_root = tmp_path / ".claude" / "skills"
    codex_root = tmp_path / ".codex" / "skills"

    result = install_canonical_skill(roots=[claude_root, codex_root])

    assert result["installed"] == [
        str(claude_root / "sibyl"),
        str(codex_root / "sibyl"),
    ]
    assert (claude_root / "sibyl" / "SKILL.md").read_text() == canonical_skill_markdown()
    assert (codex_root / "sibyl" / "SKILL.md").read_text() == canonical_skill_markdown()


def test_skill_install_skips_symlink_without_force(tmp_path: Path) -> None:
    root = tmp_path / ".codex" / "skills"
    root.mkdir(parents=True)
    source = tmp_path / "source-skill"
    source.mkdir()
    target = root / "sibyl"
    target.symlink_to(source)

    result = install_canonical_skill(roots=[root])

    assert result["skipped"] == [str(target)]
    assert target.is_symlink()


def test_skill_install_option_uses_default_roots(tmp_path: Path) -> None:
    root = tmp_path / ".codex" / "skills"

    with patch("sibyl_cli.skill.default_skill_roots", return_value=[root]):
        result = CliRunner().invoke(app, ["--install", "--quiet"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert (root / "sibyl" / "SKILL.md").read_text() == canonical_skill_markdown()


def test_skill_install_subcommand_uses_default_roots(tmp_path: Path) -> None:
    root = tmp_path / ".codex" / "skills"

    with patch("sibyl_cli.skill.default_skill_roots", return_value=[root]):
        result = CliRunner().invoke(app, ["install", "--quiet"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert (root / "sibyl" / "SKILL.md").read_text() == canonical_skill_markdown()
