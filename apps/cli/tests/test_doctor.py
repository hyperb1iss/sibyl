from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sibyl_cli import config_store, pending_writes
from sibyl_cli import doctor as doctor_module
from sibyl_cli.doctor import DoctorCheck, DoctorContext
from sibyl_cli.main import app


def _use_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)


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


def test_doctor_reports_non_utf8_config_without_rewriting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.ensure_config_dir()
    corrupt = b'[server]\nurl = "\xff"\n'
    config_store.config_path().write_bytes(corrupt)

    checks, context = doctor_module._load_config_context()

    assert context is None
    assert [(check.name, check.status) for check in checks] == [("config", "fail")]
    assert checks[0].message == "Sibyl config is unreadable."
    assert config_store.config_path().read_bytes() == corrupt


def test_doctor_embedded_lock_detects_stale_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "embedded-surreal.lock"
    lock_path.write_text("pid = 424242\n")

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
        "pending-writes",
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
        '/home/user/.claude/hooks/sibyl/session-start.py"}]}]}}',
        encoding="utf-8",
    )
    from sibyl_cli import setup as setup_module

    monkeypatch.setattr(setup_module, "CLAUDE_HOOKS_DIR", Path("/home/user/.claude/hooks/sibyl"))
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)

    check = doctor_module._check_session_hook()
    assert check.status == "pass"


MISSHAPEN_SETTINGS = [
    '{"hooks": []}',
    '{"hooks": null}',
    '{"hooks": {"SessionStart": {"matcher": "startup"}}}',
    '{"hooks": {"UserPromptSubmit": [{"hooks": "python3 sibyl.py"}]}}',
    "[1, 2, 3]",
    "{ not json",
]


@pytest.mark.parametrize("content", MISSHAPEN_SETTINGS)
def test_hook_checks_fail_cleanly_on_misshapen_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)
    monkeypatch.setattr(doctor_module, "LEGACY_USER_PROMPT_HOOK", tmp_path / "missing.py")

    for check in (doctor_module._check_session_hook(), doctor_module._check_no_legacy_hook()):
        assert check.status == "fail"
        assert str(settings_file) in (check.detail or "")
    assert settings_file.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("content", MISSHAPEN_SETTINGS)
def test_hook_registration_refuses_to_rewrite_misshapen_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    from sibyl_cli import setup as setup_module

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(setup_module, "CLAUDE_SETTINGS_FILE", settings_file)

    assert setup_module.configure_claude_hooks() is False
    assert settings_file.read_text(encoding="utf-8") == content
    assert list(tmp_path.glob("settings.json.*.bak")) == []


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
        '"python3 /home/user/.claude/hooks/sibyl/user-prompt-submit.py"}]}]}}',
        encoding="utf-8",
    )
    from sibyl_cli import setup as setup_module

    monkeypatch.setattr(setup_module, "CLAUDE_HOOKS_DIR", Path("/home/user/.claude/hooks/sibyl"))
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)
    monkeypatch.setattr(doctor_module, "LEGACY_USER_PROMPT_HOOK", tmp_path / "missing.py")

    check = doctor_module._check_no_legacy_hook()
    assert check.status == "fail"
    assert "settings.json still registers" in (check.detail or "")


def test_agent_prompt_has_bridges_recognises_canonical_snippet() -> None:
    text = "## Sibyl\n\n### Intent → Verb Bridges\n\n- recall, remember, reflect\n"
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


def _hook_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: dict) -> Path:
    from sibyl_cli import setup as setup_module

    settings_file = tmp_path / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(content), encoding="utf-8")
    monkeypatch.setattr(setup_module, "CLAUDE_SETTINGS_FILE", settings_file)
    monkeypatch.setattr(setup_module, "CLAUDE_HOOKS_DIR", tmp_path / ".claude" / "hooks" / "sibyl")
    monkeypatch.setattr(doctor_module, "CLAUDE_SETTINGS_PATH", settings_file)
    return settings_file


def _commands(settings_file: Path, event: str) -> list[list[str]]:
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    return [[hook["command"] for hook in group["hooks"]] for group in data["hooks"][event]]


USER_POLICY = {"type": "command", "command": "/opt/sibyl-policy/check-security"}
USER_LINT = {"type": "command", "command": "npx sibyl-lint --fast"}


def test_hook_registration_keeps_user_hooks_that_mention_sibyl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_cli import setup as setup_module

    settings_file = _hook_settings(
        tmp_path,
        monkeypatch,
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [USER_POLICY, USER_LINT]}]}},
    )

    assert setup_module.configure_claude_hooks() is True

    assert _commands(settings_file, "PreToolUse") == [
        [USER_POLICY["command"], USER_LINT["command"]]
    ]
    managed = str(setup_module.CLAUDE_HOOKS_DIR / "session-start.py")
    assert _commands(settings_file, "SessionStart") == [[f"python3 {managed}"]] * 2
    # doctor must not mistake the user's hooks for Sibyl's
    assert doctor_module._check_session_hook().status == "pass"


def test_hook_registration_keeps_the_user_hook_in_a_mixed_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_cli import setup as setup_module

    managed = {
        "type": "command",
        "command": f"python3 {tmp_path}/.claude/hooks/sibyl/session-start.py",
    }
    settings_file = _hook_settings(
        tmp_path,
        monkeypatch,
        {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [USER_POLICY, managed]}]}},
    )

    assert setup_module.configure_claude_hooks() is True

    groups = json.loads(settings_file.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert groups[0] == {"matcher": "startup", "hooks": [USER_POLICY]}
    assert [g["matcher"] for g in groups] == ["startup", "startup", "resume"]
    assert list(settings_file.parent.glob("settings.json.*.bak"))


def test_hook_registration_prunes_retired_sibyl_hooks_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_cli import setup as setup_module

    hooks_dir = f"{tmp_path}/.claude/hooks/sibyl"

    def command(script: str, **extra: object) -> dict:
        return {"type": "command", "command": f"python3 {hooks_dir}/{script}", **extra}

    legacy_prompt = {
        "type": "prompt",
        "prompt": setup_module.LEGACY_STOP_HOOK_PROMPT,
        "timeout": 45,
    }
    settings_file = _hook_settings(
        tmp_path,
        monkeypatch,
        {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [command("user-prompt-submit.py")]},
                    {"hooks": [USER_LINT]},
                ],
                "PostToolUse": [{"hooks": [command("post-tool-use.py")]}],
                "Stop": [{"hooks": [command("stop.py", timeout=5)]}, {"hooks": [legacy_prompt]}],
            }
        },
    )

    assert setup_module.configure_claude_hooks() is True

    assert _commands(settings_file, "UserPromptSubmit") == [[USER_LINT["command"]]]
    data = json.loads(settings_file.read_text(encoding="utf-8"))["hooks"]
    assert data["PostToolUse"] == []
    assert data["Stop"] == []


def test_hook_registration_keeps_a_hook_that_only_reads_a_managed_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_cli import setup as setup_module

    checksum = {
        "type": "command",
        "command": f"sha256sum {tmp_path}/.claude/hooks/sibyl/session-start.py",
    }
    settings_file = _hook_settings(
        tmp_path, monkeypatch, {"hooks": {"PreToolUse": [{"hooks": [checksum]}]}}
    )

    assert setup_module.configure_claude_hooks() is True

    assert _commands(settings_file, "PreToolUse") == [[checksum["command"]]]


def test_the_installed_hook_is_recognized_as_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_cli import setup as setup_module

    _hook_settings(tmp_path, monkeypatch, {})
    installed = setup_module.get_sibyl_hooks_config()

    for groups in installed.values():
        for group in groups:
            assert all(setup_module.is_managed_hook(hook) for hook in group["hooks"])


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'sha256sum ~/.claude/hooks/sibyl/session-start.py'",
        "python3 --version ~/.claude/hooks/sibyl/session-start.py",
        "sha256sum ~/.claude/hooks/sibyl/session-start.py",
        "cat ~/.claude/hooks/sibyl/session-start.py",
    ],
)
def test_hook_registration_keeps_every_command_that_is_not_a_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from sibyl_cli import setup as setup_module

    user_hook = {"type": "command", "command": command}
    settings_file = _hook_settings(
        tmp_path, monkeypatch, {"hooks": {"PreToolUse": [{"hooks": [user_hook]}]}}
    )

    assert setup_module.configure_claude_hooks() is True

    assert _commands(settings_file, "PreToolUse") == [[command]]
    assert list(settings_file.parent.glob("settings.json.*.bak"))


def test_hook_registration_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sibyl_cli import setup as setup_module

    settings_file = _hook_settings(
        tmp_path,
        monkeypatch,
        {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [USER_POLICY]}]}},
    )

    assert setup_module.configure_claude_hooks() is True
    first = settings_file.read_text(encoding="utf-8")
    assert setup_module.configure_claude_hooks() is True

    assert settings_file.read_text(encoding="utf-8") == first
    assert json.loads(first)["model"] == "opus"


HOME = "/Users/ada"
HOOKS_DIR = Path(HOME) / ".claude" / "hooks" / "sibyl"
MANAGED = f"{HOME}/.claude/hooks/sibyl"

MATCHER_CASES = [
    # Every template the installer has written.
    (f"python3 {MANAGED}/session-start.py", True),
    (f"python3 {MANAGED}/user-prompt-submit.py", True),
    (f"python3 {MANAGED}/post-tool-use.py", True),
    (f"python3 {MANAGED}/stop.py", True),
    # The same template after light normalization.
    (f"  python3   {MANAGED}/session-start.py  ", True),
    ("python3 ~/.claude/hooks/sibyl/session-start.py", True),
    ("python3 $HOME/.claude/hooks/sibyl/session-start.py", True),
    ("python3 ${HOME}/.claude/hooks/sibyl/session-start.py", True),
    (f"python3 {HOME}//.claude/hooks/sibyl/./session-start.py", True),
    (f"\tpython3\t{MANAGED}/session-start.py \t", True),
    # Whitespace a shell keeps in a filename names a different file.
    (f"python3 {MANAGED}/session-start.py\u00a0", False),
    (f"python3 {MANAGED}/session-start.py\r", False),
    (f"python3 {MANAGED}/session-start.py\n", False),
    (f"python3\u00a0{MANAGED}/session-start.py", False),
    (f"python3 {MANAGED}/session-start.py\x0b", False),
    # Anything else is the user's, however it touches the path.
    ("bash -c 'sha256sum ~/.claude/hooks/sibyl/session-start.py'", False),
    (f"python3 --version {MANAGED}/session-start.py", False),
    (f"sha256sum {MANAGED}/session-start.py", False),
    (f"cat {MANAGED}/session-start.py", False),
    (f"/usr/bin/env python3 {MANAGED}/session-start.py", False),
    (f"python3.13 -u {MANAGED}/session-start.py", False),
    (f"uv run python {MANAGED}/session-start.py", False),
    (f"bash {MANAGED}/session-start.py", False),
    (f"{MANAGED}/session-start.py", False),
    (f"python3 {MANAGED}/session-start.py --verbose", False),
    ("python3 /home/bob/.claude/hooks/sibyl/session-start.py", False),
    (f"python3 {MANAGED}/my-own-script.py", False),
    ("/opt/sibyl-policy/check-security", False),
    ("echo 'unterminated", False),
    ("", False),
]


@pytest.mark.parametrize(("command", "managed"), MATCHER_CASES)
def test_managed_hooks_are_exact_installer_templates(command: str, managed: bool) -> None:
    from sibyl_cli import setup as setup_module

    hook = {"type": "command", "command": command}
    assert setup_module.is_managed_hook(hook, HOOKS_DIR) is managed


def test_the_legacy_stop_prompt_hook_is_managed_and_nothing_else_is() -> None:
    from sibyl_cli import setup as setup_module

    legacy = setup_module.LEGACY_STOP_HOOK_PROMPT
    reflowed = "\n".join(line.strip() for line in legacy.splitlines())
    for prompt, managed in [(legacy, True), (reflowed, True), ("Summarize the session.", False)]:
        hook = {"type": "prompt", "prompt": prompt, "timeout": 45}
        assert setup_module.is_managed_hook(hook, HOOKS_DIR) is managed


def test_every_template_the_installer_writes_is_managed() -> None:
    from sibyl_cli import setup as setup_module

    for script in setup_module.MANAGED_HOOK_SCRIPTS:
        command = setup_module.managed_hook_command(HOOKS_DIR, script)
        assert setup_module.is_managed_hook({"type": "command", "command": command}, HOOKS_DIR)


def _repo_hooks_configure():
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "hooks" / "configure.py"
    spec = importlib.util.spec_from_file_location("sibyl_repo_hooks_configure", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cli_and_the_repo_hooks_script_share_one_template_list() -> None:
    from sibyl_cli import setup as setup_module

    repo = _repo_hooks_configure()
    assert repo.MANAGED_HOOK_SCRIPTS == setup_module.MANAGED_HOOK_SCRIPTS
    assert repo.LEGACY_STOP_HOOK_PROMPT == setup_module.LEGACY_STOP_HOOK_PROMPT
    for script in setup_module.MANAGED_HOOK_SCRIPTS:
        assert repo.managed_hook_command(HOOKS_DIR, script) == (
            setup_module.managed_hook_command(HOOKS_DIR, script)
        )
    for command, managed in MATCHER_CASES:
        hook = {"type": "command", "command": command}
        assert repo.is_managed_hook(hook, HOOKS_DIR) is managed, command
