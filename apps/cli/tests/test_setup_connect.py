"""`sibyl setup <url>`: one command that connects a machine to a server."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from sibyl_cli import config_store, connect, doctor, setup, state
from sibyl_cli.main import app

# The server fixture replaces connect.login; one test needs the real one.
connect.__dict__.setdefault("_real_login", connect.login)

SERVER = "https://sibyl.example.com"


class FakeServer:
    """Stands in for the network: health probe, identity, and browser login."""

    def __init__(self, *, version: str = "1.4.1", minimum: str | None = None) -> None:
        self.version = version
        self.minimum = minimum
        self.signed_in: str | None = None
        self.logins: list[str] = []
        self.probes: list[tuple[str, bool]] = []
        self.reachable = True

    def probe(self, server_url: str, *, insecure: bool) -> tuple[str | None, str | None]:
        self.probes.append((server_url, insecure))
        if not self.reachable:
            raise httpx.ConnectError("Connection refused")
        return self.version, self.minimum

    def whoami(self, ctx: config_store.Context) -> str | None:
        return self.signed_in

    def login(self, ctx: config_store.Context) -> None:
        self.logins.append(ctx.name)
        self.signed_in = "ada@example.com"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    # `-C` stores a process-wide override; keep it from leaking between tests.
    monkeypatch.setattr(state, "_context_override", None)
    monkeypatch.setattr(state, "_ignore_selection", False)
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


def _flat(output: str) -> str:
    """Output with Rich's line wrapping undone."""
    return " ".join(output.split())


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
    assert "not valid Claude settings; left unchanged" in _flat(result.output)
    assert settings_file.read_text() == "{ not json"


@pytest.mark.parametrize(
    "content",
    [
        '{"hooks": []}',
        '{"hooks": null}',
        '{"hooks": {"SessionStart": {"matcher": "startup"}}}',
        '{"hooks": {"SessionStart": ["not an object"]}}',
        '["not", "an", "object"]',
    ],
)
def test_setup_reports_misshapen_claude_settings_without_touching_them(
    home: Path, server: FakeServer, content: str
) -> None:
    settings_file = home / ".claude" / "settings.json"
    settings_file.parent.mkdir()
    settings_file.write_text(content)

    result = _run(SERVER, "--yes")

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "not valid Claude settings; left unchanged" in _flat(result.output)
    assert str(settings_file) in result.output.replace("\n", "")
    assert settings_file.read_text() == content
    assert list(settings_file.parent.glob("settings.json.*.bak")) == []


@pytest.mark.parametrize(
    "url",
    ["http://sibyl.example.com", "http://localtest.me:13693", "http://10.0.0.5:3334"],
)
def test_plain_http_to_another_machine_is_refused_before_anything_runs(
    home: Path, server: FakeServer, url: str
) -> None:
    result = _run(url, "--yes")

    assert result.exit_code == 1
    assert "--insecure" in result.output
    assert server.probes == []
    assert server.logins == []
    assert config_store.list_contexts() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3334",
        "http://127.0.0.1:3334",
        "http://127.8.0.1:9000",
        "http://[::1]:3334",
    ],
)
def test_plain_http_to_this_machine_is_allowed(home: Path, server: FakeServer, url: str) -> None:
    result = _run(url, "--yes")

    assert result.exit_code == 0, result.output
    assert server.probes == [(url, False)]


def test_plain_http_to_another_machine_is_allowed_with_insecure(
    home: Path, server: FakeServer
) -> None:
    result = _run("http://sibyl.lan:3334", "--yes", "--insecure")

    assert result.exit_code == 0, result.output
    assert server.probes == [("http://sibyl.lan:3334", True)]


def test_a_secure_context_never_inherits_insecure_from_a_sibling(
    home: Path, server: FakeServer
) -> None:
    config_store.create_context("local", server_url="https://sibyl.local", set_active=True)
    config_store.create_context("lab", server_url="https://sibyl.local", insecure=True)
    server.signed_in = "ada@example.com"

    result = _run("https://sibyl.local", "--yes")

    assert result.exit_code == 0, result.output
    assert server.probes == [("https://sibyl.local", False)]
    secure = config_store.get_context("local")
    assert secure is not None
    assert secure.insecure is False
    assert "skips TLS verification" not in result.output


def test_a_context_that_already_skips_verification_keeps_doing_so(
    home: Path, server: FakeServer
) -> None:
    config_store.create_context("lab", server_url="https://sibyl.local", insecure=True)
    server.signed_in = "ada@example.com"

    result = _run("https://sibyl.local", "--yes")

    assert result.exit_code == 0, result.output
    assert server.probes == [("https://sibyl.local", True)]


def test_a_failed_device_login_leaves_the_previous_context_active(
    home: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.create_context("prod", server_url="https://prod.example.com", set_active=True)
    # Run the real login against a server whose device endpoint is not implemented.
    monkeypatch.setattr(connect, "login", connect.__dict__["_real_login"])

    def device_501(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(501, json={"detail": "nope"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", device_501)

    result = _run(SERVER, "--yes")

    assert result.exit_code == 1
    assert "sign-in did not complete" in result.output
    assert config_store.get_active_context_name() == "prod"
    assert [ctx.name for ctx in config_store.list_contexts()] == ["prod"]


def test_a_failed_login_does_not_switch_to_an_existing_context(
    home: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.create_context("prod", server_url="https://prod.example.com", set_active=True)
    config_store.create_context("team", server_url=SERVER)
    monkeypatch.setattr(connect, "login", lambda _ctx: None)

    result = _run(SERVER, "--yes")

    assert result.exit_code == 1
    assert config_store.get_active_context_name() == "prod"
    assert config_store.get_context("team") is not None


def test_setup_without_a_url_uses_the_active_context(home: Path, server: FakeServer) -> None:
    config_store.create_context("team", server_url=SERVER, set_active=True)
    server.signed_in = "ada@example.com"

    result = _run("--yes")

    assert result.exit_code == 0, result.output
    assert SERVER in result.output
    assert "team (active)" in result.output


def test_setup_without_a_url_or_context_sets_up_the_local_server(
    home: Path, server: FakeServer
) -> None:
    result = _run("--yes")

    assert result.exit_code == 0, result.output
    assert server.probes == [("http://localhost:3334", False)]
    assert "local (created, active)" in result.output


def test_unreachable_default_server_says_to_pass_a_url(home: Path, server: FakeServer) -> None:
    server.reachable = False

    result = _run("--yes")

    assert result.exit_code == 1
    assert "unreachable" in result.output
    assert "sibyl setup https://your-sibyl-host" in result.output
    assert config_store.list_contexts() == []


def test_a_second_local_server_gets_a_port_named_context(home: Path, server: FakeServer) -> None:
    config_store.create_context("local", server_url="http://localhost:3337", set_active=True)

    result = _run("http://localhost:3334", "--yes")

    assert result.exit_code == 0, result.output
    assert "local-3334 (created, active)" in result.output
    local = config_store.get_context("local")
    assert local is not None
    assert local.server_url == "http://localhost:3337"


def test_insecure_flag_reaches_the_probe_and_the_context(home: Path, server: FakeServer) -> None:
    result = _run("https://sibyl.local", "--yes", "--insecure")

    assert result.exit_code == 0, result.output
    assert server.probes == [("https://sibyl.local", True)]
    ctx = config_store.get_context("sibyl.local")
    assert ctx is not None
    assert ctx.insecure is True


def test_setup_warns_when_a_pinned_context_still_wins(
    home: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.create_context("prod", server_url="https://prod.example.com", set_active=True)
    monkeypatch.setattr(config_store, "resolve_context_from_cwd", lambda: "prod")

    result = _run(SERVER, "--yes")

    assert result.exit_code == 0, result.output
    assert "sibyl.example.com (created, active)" in result.output
    assert "selects context 'prod'" in result.output


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


def test_setup_without_a_url_honors_the_context_flag(home: Path, server: FakeServer) -> None:
    config_store.create_context("local", server_url="http://localhost:3334", set_active=True)
    config_store.create_context("team", server_url=SERVER)
    server.signed_in = "ada@example.com"

    result = CliRunner().invoke(app, ["-C", "team", "setup", "--yes"])

    assert result.exit_code == 0, result.output
    assert f"Sibyl setup {SERVER}" in result.output
    assert "team (active)" in result.output


def test_probe_reads_version_and_floor_from_the_public_health_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        requests.append(url)
        return httpx.Response(
            200,
            json={"status": "healthy", "version": "1.4.1"},
            headers={"X-Sibyl-Version": "1.4.1", "X-Sibyl-Min-Client": "1.4.0"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(connect.httpx, "get", fake_get)

    assert connect.probe_server(SERVER, insecure=False) == ("1.4.1", "1.4.0")
    assert requests == [f"{SERVER}/api/health"]


def test_whoami_treats_a_rejected_login_as_signed_out(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = config_store.create_context("team", server_url=SERVER)

    async def rejected(self: object, path: str, **kwargs: object) -> dict:
        raise connect.SibylClientError("Unauthorized", status_code=401)

    monkeypatch.setattr(connect.SibylClient, "get", rejected)

    assert connect.whoami(ctx) is None
