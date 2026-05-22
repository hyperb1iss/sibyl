from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sibyl_cli import config_store
from sibyl_cli import doctor as doctor_module
from sibyl_cli.doctor import DoctorCheck, DoctorContext
from sibyl_cli.main import app


def _use_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)


def test_doctor_json_reports_missing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)

    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    assert '"ok": false' in result.stdout
    assert "No Sibyl config exists" in result.stdout


def test_doctor_fails_when_active_context_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.ensure_config_dir()
    config_store.config_path().write_text('active_context = "ghost"\n')

    checks, context = doctor_module._load_config_context()

    assert context is None
    assert any(check.name == "context" and check.status == "fail" for check in checks)


def test_doctor_embedded_lock_detects_stale_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "embedded-surreal.lock"
    lock_path.write_text('pid = 424242\n')

    check = doctor_module._check_embedded_lock(
        lock_path=lock_path,
        pid_alive=lambda _pid: False,
    )

    assert check.status == "fail"
    assert "stale" in check.message


@pytest.mark.asyncio
async def test_doctor_collects_healthy_local_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.create_context(
        "local",
        "http://localhost:3334",
        set_active=True,
    )

    async def health(_context: DoctorContext, _timeout: float) -> DoctorCheck:
        return DoctorCheck("daemon", "pass", "Sibyl API is healthy.")

    async def write_probe(_enabled: bool) -> DoctorCheck:
        return DoctorCheck("write-test", "pass", "Authenticated write probe succeeded.")

    monkeypatch.setattr(doctor_module, "_check_public_health", health)
    monkeypatch.setattr(doctor_module, "_check_write_probe", write_probe)
    monkeypatch.setattr(doctor_module, "_probe_port", lambda *_args: True)

    checks = await doctor_module.collect_doctor_checks(
        timeout=0.1, write_test=True, skip_agent=True
    )

    assert not any(check.failed for check in checks)
    assert [check.name for check in checks] == [
        "config",
        "context",
        "daemon",
        "port",
        "embedded-lock",
        "write-test",
    ]


# ---------- agent-setup checks --------------------------------------------


def _install_canonical_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    roots: list[Path],
    canonical: str,
    installed: dict[Path, str] | None = None,
) -> None:
    """Stage skill stub fixtures: canonical content + installed copies per root."""
    monkeypatch.setattr(doctor_module, "canonical_skill_markdown", lambda: canonical)
    monkeypatch.setattr(doctor_module, "default_skill_roots", lambda: roots)
    if installed is None:
        return
    for root, content in installed.items():
        target = root / "sibyl"
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(content, encoding="utf-8")


def test_check_skill_stub_passes_when_canonical_installed_everywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = [tmp_path / "a" / "skills", tmp_path / "b" / "skills"]
    canonical = "# Sibyl stub\n"
    _install_canonical_stub(
        monkeypatch,
        roots=roots,
        canonical=canonical,
        installed={r: canonical for r in roots},
    )

    check = doctor_module._check_skill_stub()
    assert check.status == "pass"
    assert "2 assistant roots" in check.message


def test_check_skill_stub_fails_when_installed_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a" / "skills"
    _install_canonical_stub(
        monkeypatch,
        roots=[root],
        canonical="# Sibyl stub v2\n",
        installed={root: "# Sibyl stub v1\n"},
    )

    check = doctor_module._check_skill_stub()
    assert check.status == "fail"
    assert "out of date" in check.message
    assert "sibyl skill install --force" in (check.detail or "")


def test_check_skill_stub_fails_when_completely_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "a" / "skills"
    _install_canonical_stub(monkeypatch, roots=[root], canonical="# canon\n", installed={})

    check = doctor_module._check_skill_stub()
    assert check.status == "fail"
    assert "not installed" in check.message


def test_check_session_hook_passes_when_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"command": "python3 '
        '/Users/bliss/.claude/hooks/sibyl/session-start.py"}]}]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)

    check = doctor_module._check_session_hook()
    assert check.status == "pass"


def test_check_session_hook_warns_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"hooks": {}}', encoding="utf-8")
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)

    check = doctor_module._check_session_hook()
    assert check.status == "warn"
    assert "not registered" in check.message


def test_check_no_legacy_hook_passes_when_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"hooks": {"SessionStart": []}}', encoding="utf-8")
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)
    monkeypatch.setattr(doctor_module, "LEGACY_USER_PROMPT_HOOK", tmp_path / "missing.py")

    check = doctor_module._check_no_legacy_hook()
    assert check.status == "pass"


def test_check_no_legacy_hook_fails_when_orphan_file_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"hooks": {}}', encoding="utf-8")
    orphan = tmp_path / "user-prompt-submit.py"
    orphan.write_text("# legacy", encoding="utf-8")
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)
    monkeypatch.setattr(doctor_module, "LEGACY_USER_PROMPT_HOOK", orphan)

    check = doctor_module._check_no_legacy_hook()
    assert check.status == "fail"
    assert "orphan script" in (check.detail or "")


def test_check_no_legacy_hook_fails_when_settings_still_have_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        '{"hooks": {"UserPromptSubmit": [{"hooks": [{"command": '
        '"python3 /Users/bliss/.claude/hooks/sibyl/user-prompt-submit.py"}]}]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)
    monkeypatch.setattr(doctor_module, "LEGACY_USER_PROMPT_HOOK", tmp_path / "missing.py")

    check = doctor_module._check_no_legacy_hook()
    assert check.status == "fail"
    assert "settings.json still registers" in (check.detail or "")


def test_agent_prompt_has_bridges_recognises_canonical_snippet() -> None:
    text = (
        "## Sibyl\n\n"
        "### Intent → Verb Bridges\n\n"
        "- recall, remember, reflect\n"
    )
    assert doctor_module._agent_prompt_has_bridges(text) is True


def test_agent_prompt_has_bridges_rejects_loop_only_content() -> None:
    text = "Just a CLAUDE.md that mentions recall and remember but no bridges."
    assert doctor_module._agent_prompt_has_bridges(text) is False


def test_check_agent_prompt_content_passes_when_file_has_bridges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        "### Intent -> Verb Bridges\nrecall remember reflect\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor_module, "AGENT_PROMPT_CANDIDATES", (target,))

    check = doctor_module._check_agent_prompt_content()
    assert check.status == "pass"
    assert "Recommended memory-loop content found" in check.message


def test_check_agent_prompt_content_warns_when_file_lacks_bridges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text("Just a generic CLAUDE.md\n", encoding="utf-8")
    monkeypatch.setattr(doctor_module, "AGENT_PROMPT_CANDIDATES", (target,))

    check = doctor_module._check_agent_prompt_content()
    assert check.status == "warn"
    assert "does not include the recommended bridges" in check.message


def test_check_agent_prompt_content_warns_when_no_file_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_module, "AGENT_PROMPT_CANDIDATES", (tmp_path / "absent.md",))

    check = doctor_module._check_agent_prompt_content()
    assert check.status == "warn"
    assert "No CLAUDE.md or AGENTS.md found" in check.message


def test_append_managed_block_creates_new_block_when_file_missing(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"

    action = doctor_module.append_managed_block(target, snippet="hello world")

    assert action == "appended"
    content = target.read_text(encoding="utf-8")
    assert doctor_module.AGENT_BLOCK_BEGIN in content
    assert doctor_module.AGENT_BLOCK_END in content
    assert "hello world" in content


def test_append_managed_block_updates_existing_block_in_place(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        "# Preexisting\n\n"
        f"{doctor_module.AGENT_BLOCK_BEGIN}\nold block\n{doctor_module.AGENT_BLOCK_END}\n"
        "\n# Trailing content\n",
        encoding="utf-8",
    )

    action = doctor_module.append_managed_block(target, snippet="new block")

    assert action == "updated"
    content = target.read_text(encoding="utf-8")
    assert "old block" not in content
    assert "new block" in content
    assert "# Preexisting" in content
    assert "# Trailing content" in content
    # Markers should appear exactly once each
    assert content.count(doctor_module.AGENT_BLOCK_BEGIN) == 1
    assert content.count(doctor_module.AGENT_BLOCK_END) == 1
