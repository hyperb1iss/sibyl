"""A team-server login must not redirect this machine or land in a personal org.

Two traps from a real team migration: logging in to a team server with
``--context`` silently made it the active context for every session on the
machine, and the token it saved acted in the caller's auto-created personal
org, so projects and migrated memories landed where the team could not see
them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sibyl_cli import auth, auth_store, config_store
from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.main import app

TEAM_SERVER = "https://sibyl.team.example"
PERSONAL = {"id": "org-personal", "slug": "u-alice", "name": "Alice", "is_personal": True}
TEAM = {"id": "org-team", "slug": "acme", "name": "Acme", "is_personal": False}
OTHER_TEAM = {"id": "org-other", "slug": "globex", "name": "Globex", "is_personal": False}
SAVED_TOKEN = "token-saved-by-this-login"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "sibyl_cli.pending_identity.warm_pending_replay_identity", lambda *_a, **_k: None
    )
    return tmp_path


def _fake_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: dict[str, Any],
    orgs: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Route the client's requests to a fake team server; returns the call log."""
    calls: list[tuple[str, str]] = []

    async def _request(self: SibylClient, method: str, path: str, **_kwargs: Any) -> dict:
        calls.append((method, path))
        # Every settlement request must carry the credential this login saved.
        assert self.auth_token == SAVED_TOKEN, self.auth_token
        if (method, path) == ("GET", "/auth/me"):
            return {"user": {"email": "alice@acme.test"}, "organization": current}
        if (method, path) == ("GET", "/orgs"):
            return {"orgs": orgs}
        if method == "POST" and path.startswith("/orgs/") and path.endswith("/switch"):
            slug = path.split("/")[2]
            org = next(o for o in orgs if o["slug"] == slug)
            return {
                "organization": {k: org[k] for k in ("id", "slug", "name")},
                "access_token": f"token-for-{slug}",
                "refresh_token": f"refresh-for-{slug}",
                "expires_in": 3600,
            }
        raise SibylClientError(f"unexpected {method} {path}", status_code=500)

    monkeypatch.setattr(SibylClient, "_request", _request)
    monkeypatch.setattr(auth, "_login_auto", _saving_login)
    return calls


def _saving_login(*, api_url: str, credential_scope_name: str | None, **_kwargs: Any) -> bool:
    auth_store.set_tokens(api_url, SAVED_TOKEN, credential_scope=credential_scope_name)
    return True


def _login(*argv: str) -> Any:
    return CliRunner().invoke(app, ["auth", "login", *argv])


def test_new_team_context_leaves_the_active_context_alone(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    _fake_server(monkeypatch, current=TEAM, orgs=[PERSONAL, TEAM])

    result = _login(TEAM_SERVER, "--context", "team")

    assert result.exit_code == 0, result.stdout
    assert config_store.get_active_context_name() == "local"
    assert config_store.get_context("team") is not None
    assert "the active context is still 'local'" in result.stdout


def test_use_flag_activates_the_new_context(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    _fake_server(monkeypatch, current=TEAM, orgs=[PERSONAL, TEAM])

    result = _login(TEAM_SERVER, "--context", "team", "--use")

    assert result.exit_code == 0, result.stdout
    assert config_store.get_active_context_name() == "team"


def test_first_context_on_a_fresh_machine_becomes_active(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_server(monkeypatch, current=TEAM, orgs=[TEAM])

    result = _login(TEAM_SERVER, "--context", "team")

    assert result.exit_code == 0, result.stdout
    assert config_store.get_active_context_name() == "team"


def test_personal_login_moves_to_the_only_team_org_and_pins_it(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    calls = _fake_server(monkeypatch, current=PERSONAL, orgs=[PERSONAL, TEAM])

    result = _login(TEAM_SERVER, "--context", "team")

    assert result.exit_code == 0, result.stdout
    assert ("POST", "/orgs/acme/switch") in calls
    ctx = config_store.get_context("team")
    assert ctx is not None and ctx.org_slug == "acme"
    # The switched token is stored under the scope the pinned context reads.
    stored = auth_store.read_server_credentials(
        f"{TEAM_SERVER}/api", credential_scope=auth_store.credential_scope("team", "acme")
    )
    assert stored.get("access_token") == "token-for-acme"
    assert "Using org Acme (acme)" in result.stdout


def test_several_team_orgs_are_offered_not_guessed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_server(monkeypatch, current=PERSONAL, orgs=[PERSONAL, TEAM, OTHER_TEAM])

    result = _login(TEAM_SERVER, "--context", "team")

    assert result.exit_code == 0, result.stdout
    assert not any(path.endswith("/switch") for _method, path in calls)
    ctx = config_store.get_context("team")
    assert ctx is not None and ctx.org_slug is None
    assert "acme, globex" in result.stdout
    assert "sibyl -C team org switch <slug>" in result.stdout


def test_a_pinned_context_org_is_switched_to_after_login(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.create_context("team", TEAM_SERVER, org_slug="acme", set_active=True)
    calls = _fake_server(monkeypatch, current=PERSONAL, orgs=[PERSONAL, TEAM, OTHER_TEAM])

    result = _login(TEAM_SERVER, "--context", "team")

    assert result.exit_code == 0, result.stdout
    assert ("POST", "/orgs/acme/switch") in calls
    stored = auth_store.read_server_credentials(
        f"{TEAM_SERVER}/api", credential_scope=auth_store.credential_scope("team", "acme")
    )
    assert stored.get("access_token") == "token-for-acme"


def test_a_login_already_in_a_team_org_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_server(monkeypatch, current=TEAM, orgs=[PERSONAL, TEAM])

    result = _login(TEAM_SERVER, "--context", "team")

    assert result.exit_code == 0, result.stdout
    assert not any(path.endswith("/switch") for _method, path in calls)
    ctx = config_store.get_context("team")
    assert ctx is not None and ctx.org_slug is None


def test_a_single_user_server_with_only_a_personal_org_is_left_alone(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_server(monkeypatch, current=PERSONAL, orgs=[PERSONAL])

    result = _login("http://localhost:3334", "--context", "local")

    assert result.exit_code == 0, result.stdout
    assert not any(path.endswith("/switch") for _method, path in calls)


def test_no_browser_login_that_saved_nothing_skips_the_org_check(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_server(monkeypatch, current=PERSONAL, orgs=[PERSONAL, TEAM])
    monkeypatch.setattr(auth, "_login_auto", lambda **_kwargs: False)

    result = _login(TEAM_SERVER, "--context", "team", "--no-browser")

    assert result.exit_code == 0, result.stdout
    assert calls == []


def test_an_unreachable_org_listing_warns_without_failing_the_login(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _request(self: SibylClient, method: str, path: str, **_kwargs: Any) -> dict:
        raise SibylClientError("API error: forbidden", status_code=403)

    monkeypatch.setattr(SibylClient, "_request", _request)
    monkeypatch.setattr(auth, "_login_auto", _saving_login)

    result = _login(TEAM_SERVER, "--context", "team")

    assert result.exit_code == 0, result.stdout
    assert "could not confirm which org this login uses" in result.stdout


def test_an_ambient_environment_token_never_drives_the_org_switch(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another user's token in the environment would otherwise authenticate the
    # identity check and the switch, filing that user's credential here.
    monkeypatch.setenv("SIBYL_AUTH_TOKEN", "someone-elses-token")
    calls = _fake_server(monkeypatch, current=PERSONAL, orgs=[PERSONAL, TEAM])

    result = _login(TEAM_SERVER, "--context", "team")

    assert result.exit_code == 0, result.stdout
    assert ("POST", "/orgs/acme/switch") in calls
    stored = auth_store.read_server_credentials(
        f"{TEAM_SERVER}/api", credential_scope=auth_store.credential_scope("team", "acme")
    )
    assert stored.get("access_token") == "token-for-acme"
