"""`sibyl setup <url>`: one command that connects a machine to a server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from sibyl_cli import config_store, connect, doctor, setup
from sibyl_cli.main import app

SERVER = "https://sibyl.example.com"


class FakeServer:
    """Stands in for the network: health probe, identity, and browser login."""

    def __init__(self, *, version: str = "1.4.1", minimum: str | None = None) -> None:
        self.version = version
        self.minimum = minimum
        self.signed_in: str | None = None
        self.logins: list[str] = []

    def probe(self, server_url: str, *, insecure: bool) -> tuple[str | None, str | None]:
        return self.version, self.minimum

    def whoami(self, ctx: config_store.Context) -> str | None:
        return self.signed_in

    def login(self, ctx: config_store.Context) -> None:
        self.logins.append(ctx.name)
        self.signed_in = "ada@example.com"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup, "CLAUDE_HOOKS_DIR", tmp_path / ".claude" / "hooks" / "sibyl")
    monkeypatch.setattr(setup, "CLAUDE_SETTINGS_FILE", tmp_path / ".claude" / "settings.json")
    monkeypatch.setattr(doctor, "CLAUDE_SETTINGS_PATH", tmp_path / ".claude" / "settings.json")
    monkeypatch.setattr(connect.shutil, "which", lambda _name: None)
    return tmp_path


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(connect, "probe_server", fake.probe)
    monkeypatch.setattr(connect, "whoami", fake.whoami)
    monkeypatch.setattr(connect, "login", fake.login)
    return fake


def _with_claude_code(home: Path) -> None:
    # Claude Code writes ~/.claude.json on first run.
    (home / ".claude.json").write_text("{}")


def _run(*args: str, input: str | None = None) -> typer.testing.Result:
    return CliRunner().invoke(app, ["setup", *args], input=input)


def _sibyl_session_hooks(home: Path) -> list[dict]:
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    return [
        entry
        for entry in settings["hooks"]["SessionStart"]
        if any("sibyl" in hook["command"] for hook in entry["hooks"])
    ]


def test_setup_connects_signs_in_and_installs_skill_and_hook(
    home: Path, server: FakeServer
) -> None:
    _with_claude_code(home)

    result = _run(f"{SERVER}/", "--yes")

    assert result.exit_code == 0, result.output
    ctx = config_store.get_active_context()
    assert ctx is not None
    assert ctx.name == "sibyl.example.com"
    assert ctx.server_url == SERVER
    assert server.logins == ["sibyl.example.com"]
    for root in (".claude", ".codex", ".agents"):
        assert (home / root / "skills" / "sibyl" / "SKILL.md").exists()
    assert (home / ".claude" / "hooks" / "sibyl" / "session-start.py").exists()
    assert len(_sibyl_session_hooks(home)) == 2  # startup and resume
    assert "ada@example.com" in result.output
    assert "Claude Code SessionStart" in result.output
    assert 'Try it: sibyl context "what\'s in flight"' in result.output


def test_setup_rerun_skips_every_finished_step(home: Path, server: FakeServer) -> None:
    _with_claude_code(home)
    assert _run(SERVER, "--yes").exit_code == 0
    settings_before = (home / ".claude" / "settings.json").read_text()

    result = _run(SERVER, "--yes")

    assert result.exit_code == 0, result.output
    assert server.logins == ["sibyl.example.com"]
    assert "sibyl.example.com (active)" in result.output
    assert "already installed" in result.output
    assert "already registered" in result.output
    assert (home / ".claude" / "settings.json").read_text() == settings_before
    assert list((home / ".claude").glob("settings.json.*.bak")) == []
    assert [ctx.name for ctx in config_store.list_contexts()] == ["sibyl.example.com"]


def test_setup_refuses_a_cli_below_the_server_floor(
    home: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.minimum = "99.0.0"
    monkeypatch.setattr(connect, "upgrade_command", lambda: "brew upgrade hyperb1iss/tap/sibyl")

    result = _run(SERVER, "--yes")

    assert result.exit_code == 1
    assert "needs sibyl 99.0.0+" in result.output
    assert "brew upgrade hyperb1iss/tap/sibyl" in result.output
    assert config_store.list_contexts() == []
    assert server.logins == []


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("/opt/homebrew/Cellar/sibyl/1.4.1/libexec", "brew upgrade hyperb1iss/tap/sibyl"),
        ("/home/linuxbrew/.linuxbrew/Cellar/sibyl/1.4.1/libexec", "brew upgrade"),
        ("/home/ada/.local/share/uv/tools/sibyl-dev", "uv tool install --upgrade sibyl-dev"),
    ],
)
def test_upgrade_command_matches_how_the_cli_was_installed(
    monkeypatch: pytest.MonkeyPatch, prefix: str, expected: str
) -> None:
    monkeypatch.setattr(connect.sys, "prefix", prefix)

    assert connect.upgrade_command().startswith(expected)


def test_setup_without_a_tty_runs_without_prompting(home: Path, server: FakeServer) -> None:
    result = _run(SERVER)

    assert result.exit_code == 0, result.output
    assert "Continue?" not in result.output
    assert server.logins == ["sibyl.example.com"]


def test_interactive_setup_asks_once_and_honors_no(
    home: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connect, "interactive", lambda: True)

    declined = _run(SERVER, input="n\n")

    assert declined.exit_code == 1
    assert declined.output.count("Continue?") == 1
    assert config_store.list_contexts() == []
    assert server.logins == []

    accepted = _run(SERVER, input="\n")

    assert accepted.exit_code == 0, accepted.output
    assert accepted.output.count("Continue?") == 1
    assert "SessionStart hook" in accepted.output


def test_setup_reuses_the_context_already_pointing_at_the_server(
    home: Path, server: FakeServer
) -> None:
    config_store.create_context("local", server_url="http://localhost:3334", set_active=True)
    config_store.create_context("team", server_url=f"{SERVER}/")
    server.signed_in = "ada@example.com"

    result = _run(SERVER, "--yes")

    assert result.exit_code == 0, result.output
    assert "team (now active)" in result.output
    assert config_store.get_active_context_name() == "team"
    assert sorted(ctx.name for ctx in config_store.list_contexts()) == ["local", "team"]
    assert server.logins == []


def test_setup_will_not_repoint_a_context_named_for_another_server(
    home: Path, server: FakeServer
) -> None:
    config_store.create_context("sibyl.example.com", server_url="https://elsewhere.example.com")

    result = _run(SERVER, "--yes")

    assert result.exit_code == 1
    assert "--context <name>" in result.output
    ctx = config_store.get_context("sibyl.example.com")
    assert ctx is not None
    assert ctx.server_url == "https://elsewhere.example.com"


def test_setup_without_claude_code_skips_the_hook(home: Path, server: FakeServer) -> None:
    first = _run(SERVER, "--yes")
    # The skill install creates ~/.claude, which must not read as Claude Code.
    rerun = _run(SERVER, "--yes")

    for result in (first, rerun):
        assert result.exit_code == 0, result.output
        assert "Claude Code not found" in result.output
    assert not (home / ".claude" / "settings.json").exists()


def test_setup_no_hooks_leaves_claude_settings_alone(home: Path, server: FakeServer) -> None:
    _with_claude_code(home)

    result = _run(SERVER, "--yes", "--no-hooks")

    assert result.exit_code == 0, result.output
    assert not (home / ".claude" / "settings.json").exists()


def test_setup_never_rewrites_unreadable_claude_settings(home: Path, server: FakeServer) -> None:
    settings_file = home / ".claude" / "settings.json"
    settings_file.parent.mkdir()
    settings_file.write_text("{ not json")

    result = _run(SERVER, "--yes")

    assert result.exit_code == 1
    assert "could not update" in result.output
    assert settings_file.read_text() == "{ not json"


def test_setup_without_a_url_uses_the_active_context(home: Path, server: FakeServer) -> None:
    config_store.create_context("team", server_url=SERVER, set_active=True)
    server.signed_in = "ada@example.com"

    result = _run("--yes")

    assert result.exit_code == 0, result.output
    assert SERVER in result.output
    assert "team (active)" in result.output


def test_setup_without_a_url_or_context_asks_for_the_server(home: Path, server: FakeServer) -> None:
    result = _run("--yes")

    assert result.exit_code == 1
    assert "sibyl setup https://" in result.output


def test_failed_sign_in_reports_incomplete_setup(
    home: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connect, "login", lambda _ctx: None)

    result = _run(SERVER, "--yes")

    assert result.exit_code == 1
    assert "sign-in did not complete" in result.output
    assert "Try it" not in result.output


def test_login_waits_for_the_browser_under_the_context_credential_scope(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(connect, "_login_auto", lambda **kwargs: calls.append(kwargs))
    ctx = config_store.create_context("team", server_url=SERVER, org_slug="acme")

    connect.login(ctx)

    assert calls == [
        {
            "api_url": f"{SERVER}/api",
            "no_browser": False,
            "timeout_seconds": 600,
            "email": None,
            "password": None,
            "insecure": False,
            "credential_scope_name": "context:team:org:acme",
        }
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://sibyl.example.com/", SERVER),
        ("sibyl.example.com", SERVER),
        ("https://sibyl.example.com/api/", SERVER),
        ("http://localhost:3334", "http://localhost:3334"),
    ],
)
def test_normalize_server_url_accepts_what_people_paste(raw: str, expected: str) -> None:
    assert connect.normalize_server_url(raw) == expected


def test_normalize_server_url_rejects_other_schemes() -> None:
    with pytest.raises(typer.BadParameter):
        connect.normalize_server_url("ftp://sibyl.example.com")
