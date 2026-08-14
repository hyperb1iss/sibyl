from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from sibyl_cli import auth, auth_store, config_store
from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.main import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_top_level_auth_aliases_are_registered() -> None:
    runner = CliRunner()

    login = runner.invoke(app, ["login", "--help"])
    logout = runner.invoke(app, ["logout", "--help"])
    whoami = runner.invoke(app, ["whoami", "--help"])

    assert login.exit_code == 0
    assert "--no-browser" in _plain(login.stdout)
    assert "--break-glass-reason" in _plain(login.stdout)
    assert logout.exit_code == 0
    assert "--all" in _plain(logout.stdout)
    assert whoami.exit_code == 0
    assert "Show auth status" in _plain(whoami.stdout)


def test_device_no_browser_prints_url_without_polling(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        auth,
        "_start_device_flow",
        lambda **_kwargs: ("device-code", "USER-CODE", "https://verify.test", 5, 600),
    )

    def fail_poll(**_kwargs: object) -> dict:
        raise AssertionError("no-browser must not poll for approval")

    monkeypatch.setattr(auth, "_poll_device_token", fail_poll)

    with pytest.raises(auth._NoBrowserLoginPrinted):
        auth._login_via_device_flow(
            api_url="http://testserver/api",
            no_browser=True,
            timeout_seconds=180,
        )

    output = capsys.readouterr().out
    assert "USER-CODE" in output
    assert "https://verify.test" in output


def test_device_login_requests_cli_rest_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "device_code": "device-code",
                "user_code": "USER-CODE",
                "verification_uri": "https://verify.test",
                "interval": 5,
                "expires_in": 600,
            }

    def post(_url: str, **kwargs: object) -> Response:
        payload = kwargs.get("json")
        assert isinstance(payload, dict)
        calls.append(payload)
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", post)

    auth._start_device_flow(api_url="http://testserver/api")

    assert calls[0]["scope"] == auth.CLI_AUTH_SCOPE


def test_oauth_registration_requests_cli_rest_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 201

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"client_id": "client-id"}

    def post(_url: str, **kwargs: object) -> Response:
        payload = kwargs.get("json")
        assert isinstance(payload, dict)
        calls.append(payload)
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", post)

    auth._register_oauth_client(
        registration_endpoint="http://testserver/register",
        redirect_uri="http://127.0.0.1/callback",
    )

    assert calls[0]["scope"] == auth.CLI_AUTH_SCOPE


def test_login_auto_returns_after_no_browser_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def print_only(**_kwargs: object) -> dict:
        raise auth._NoBrowserLoginPrinted(
            "Login URL printed; approval polling skipped for --no-browser."
        )

    monkeypatch.setattr(auth, "_login_via_device_flow", print_only)

    auth._login_auto(
        api_url="http://testserver/api",
        no_browser=True,
        timeout_seconds=180,
        email=None,
        password=None,
    )

    output = capsys.readouterr().out
    assert "approval polling skipped" in output


def test_login_context_uses_existing_org_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    config_store.create_context(
        "eternia",
        "https://old.example",
        org_slug="stefanie-jane",
        set_active=True,
    )
    monkeypatch.setattr(auth, "_login_auto", lambda **kwargs: calls.append(kwargs))

    result = runner.invoke(
        app,
        [
            "auth",
            "login",
            "https://sibyl.hyperbliss.tech",
            "--context",
            "eternia",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["credential_scope_name"] == "context:eternia:org:stefanie-jane"
    ctx = config_store.get_context("eternia")
    assert ctx is not None
    assert ctx.server_url == "https://sibyl.hyperbliss.tech"
    assert ctx.org_slug == "stefanie-jane"


def test_login_auto_warns_when_env_token_overrides_saved_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writes: list[dict[str, object]] = []

    monkeypatch.setenv("SIBYL_AUTH_TOKEN", "sk_live_mcp_only")
    monkeypatch.setattr(
        auth,
        "_login_via_device_flow",
        lambda **_kwargs: {"access_token": "access-token", "refresh_token": "refresh-token"},
    )
    monkeypatch.setattr(auth, "_persist_tokens", lambda **kwargs: writes.append(kwargs))

    auth._login_auto(
        api_url="http://testserver/api",
        no_browser=False,
        timeout_seconds=180,
        email=None,
        password=None,
    )

    output = _plain(capsys.readouterr().out)
    assert writes[0]["access_token"] == "access-token"
    assert "SIBYL_AUTH_TOKEN is set and will override saved login credentials" in output


def test_login_auto_oauth_preserves_access_token_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[dict[str, object]] = []

    def device_unavailable(**_kwargs: object) -> dict:
        raise RuntimeError("device auth unavailable")

    def oauth_login(**_kwargs: object) -> tuple[str, str, str, int]:
        return "access-token", "refresh-token", "http://testserver", 3600

    def persist_tokens(**kwargs: object) -> None:
        writes.append(kwargs)

    monkeypatch.setattr(auth, "_login_via_device_flow", device_unavailable)
    monkeypatch.setattr(auth, "_oauth_pkce_login", oauth_login)
    monkeypatch.setattr(auth, "_persist_tokens", persist_tokens)

    auth._login_auto(
        api_url="http://testserver/api",
        no_browser=False,
        timeout_seconds=180,
        email=None,
        password=None,
    )

    assert writes == [
        {
            "api_url": "http://testserver/api",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "credential_scope_name": None,
        }
    ]


def test_login_auto_passes_break_glass_reason_to_local_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    writes: list[dict[str, object]] = []

    def local_login(**kwargs: object) -> dict:
        calls.append(kwargs)
        return {"access_token": "access-token", "refresh_token": "refresh-token"}

    def persist_tokens(**kwargs: object) -> None:
        writes.append(kwargs)

    monkeypatch.setattr(
        auth,
        "_login_via_device_flow",
        lambda **_kwargs: pytest.fail("explicit local login must not start device flow"),
    )
    monkeypatch.setattr(
        auth,
        "_login_via_oauth",
        lambda **_kwargs: pytest.fail("explicit local login must not start OAuth"),
    )
    monkeypatch.setattr(auth, "_login_via_local_password", local_login)
    monkeypatch.setattr(auth, "_persist_tokens", persist_tokens)

    auth._login_auto(
        api_url="http://testserver/api",
        no_browser=False,
        timeout_seconds=180,
        email="break-glass@example.com",
        password="super-secret",
        break_glass_reason="INC-123 IdP outage",
    )

    assert calls == [
        {
            "api_url": "http://testserver/api",
            "email": "break-glass@example.com",
            "password": "super-secret",
            "break_glass_reason": "INC-123 IdP outage",
        }
    ]
    assert writes[0]["access_token"] == "access-token"


def test_login_auto_requires_complete_local_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        auth,
        "_login_via_device_flow",
        lambda **_kwargs: pytest.fail("partial local credentials must not start device flow"),
    )
    monkeypatch.setattr(
        auth,
        "_login_via_oauth",
        lambda **_kwargs: pytest.fail("partial local credentials must not start OAuth"),
    )
    monkeypatch.setattr(
        auth,
        "_login_via_local_password",
        lambda **_kwargs: pytest.fail("partial local credentials must not call local login"),
    )

    with pytest.raises(typer.Exit) as exc:
        auth._login_auto(
            api_url="http://testserver/api",
            no_browser=False,
            timeout_seconds=180,
            email="stef@example.com",
            password=None,
        )

    assert exc.value.exit_code == 1
    assert "Local login requires both --email and --password." in _plain(capsys.readouterr().out)


def test_login_auto_exits_non_zero_when_local_login_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def rejected(**_kwargs: object) -> dict:
        raise SibylClientError("API error: invalid credentials", status_code=401)

    monkeypatch.setattr(auth, "_login_via_local_password", rejected)
    monkeypatch.setattr(
        auth,
        "_persist_tokens",
        lambda **_kwargs: pytest.fail("a rejected login must not persist tokens"),
    )

    with pytest.raises(typer.Exit) as exc:
        auth._login_auto(
            api_url="http://testserver/api",
            no_browser=False,
            timeout_seconds=180,
            email="stef@example.com",
            password="wrong",
        )

    assert exc.value.exit_code == 1
    assert "invalid credentials" in _plain(capsys.readouterr().out)


def test_auth_status_exits_non_zero_without_a_stored_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIBYL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(auth_store.Path, "home", lambda: tmp_path)

    result = CliRunner().invoke(auth.app, ["status"])

    assert result.exit_code == 1
    assert "No auth token found" in _plain(result.stdout)


def test_login_resolves_the_same_server_as_every_other_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active context outranks SIBYL_API_URL for auth exactly as it does for the client."""
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    config_store.create_context("prod", "https://sibyl.example.com", set_active=True)
    monkeypatch.setenv("SIBYL_API_URL", "http://localhost:3334/api")
    monkeypatch.setattr(auth, "_login_auto", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(app, ["auth", "login"])

    assert result.exit_code == 0
    assert calls[0]["api_url"] == "https://sibyl.example.com/api"
    assert calls[0]["api_url"] == SibylClient().base_url


def test_auth_commands_follow_the_context_override_like_the_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    config_store.create_context("prod", "https://sibyl.example.com", org_slug="acme")

    monkeypatch.setenv("SIBYL_CONTEXT", "prod")

    assert auth._compute_api_url(None) == "https://sibyl.example.com/api"
    assert auth._current_credential_scope() == "context:prod:org:acme"
    assert auth._compute_api_url(None) == SibylClient(context_name="prod").base_url


def test_an_explicit_login_url_scopes_the_credential_to_that_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pin must not scope a credential for a server the pin does not name."""
    from sibyl_cli import state

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(state, "_context_override", None)
    monkeypatch.setattr(state, "_ignore_selection", False)
    config_store.create_context("prod", "https://prod.example.com", org_slug="acme")
    config_store.create_context("staging", "https://staging.example.com", set_active=True)
    # A directory pin selects prod while the login names staging outright.
    monkeypatch.setattr(config_store, "resolve_context_from_cwd", lambda: "prod")
    monkeypatch.setattr(auth, "_login_auto", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(app, ["auth", "login", "https://staging.example.com"])

    assert result.exit_code == 0
    assert calls[0]["api_url"] == "https://staging.example.com/api"
    # The scope follows the URL that was logged in to, not the pin.
    assert calls[0]["credential_scope_name"] == "context:staging:org:default"
    assert "not the selected context" in _plain(result.stdout)


def test_an_explicit_url_with_no_matching_context_stores_unscoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_cli import state

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(state, "_context_override", None)
    monkeypatch.setattr(state, "_ignore_selection", False)
    config_store.create_context("prod", "https://prod.example.com", set_active=True)
    monkeypatch.setattr(auth, "_login_auto", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(app, ["auth", "login", "https://elsewhere.example.com"])

    assert result.exit_code == 0
    assert calls[0]["credential_scope_name"] is None


def test_a_login_with_no_explicit_url_still_uses_the_selected_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_cli import state

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(state, "_context_override", None)
    monkeypatch.setattr(state, "_ignore_selection", False)
    config_store.create_context("prod", "https://prod.example.com", org_slug="acme", set_active=True)
    monkeypatch.setattr(auth, "_login_auto", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(app, ["auth", "login"])

    assert result.exit_code == 0
    assert calls[0]["api_url"] == "https://prod.example.com/api"
    assert calls[0]["credential_scope_name"] == "context:prod:org:acme"


def test_a_shared_server_scopes_the_login_to_the_selected_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two contexts, one server: config order must not decide whose token this is."""
    from sibyl_cli import state

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(state, "_context_override", None)
    monkeypatch.setattr(state, "_ignore_selection", False)
    config_store.create_context("alpha", "https://shared.example.com", org_slug="alpha-org")
    config_store.create_context(
        "beta", "https://shared.example.com", org_slug="beta-org", set_active=True
    )
    monkeypatch.setattr(auth, "_login_auto", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(app, ["auth", "login", "https://shared.example.com"])

    assert result.exit_code == 0
    # The scope the next command reads back, not whichever context sorts first.
    assert calls[0]["credential_scope_name"] == auth._current_credential_scope()
    assert calls[0]["credential_scope_name"] == "context:beta:org:beta-org"
    # The selected context agrees with the URL, so there is nothing to warn about.
    assert "not the selected context" not in _plain(result.stdout)


def test_a_shared_server_the_selection_does_not_name_demands_disambiguation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guessing between two candidate credentials would overwrite one of them."""
    from sibyl_cli import state

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(state, "_context_override", None)
    monkeypatch.setattr(state, "_ignore_selection", False)
    config_store.create_context("alpha", "https://shared.example.com", org_slug="alpha-org")
    config_store.create_context("beta", "https://shared.example.com", org_slug="beta-org")
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    monkeypatch.setattr(auth, "_login_auto", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(app, ["auth", "login", "https://shared.example.com"])

    assert result.exit_code == 1
    assert calls == []
    plain = _plain(result.stdout)
    assert "alpha" in plain
    assert "beta" in plain
