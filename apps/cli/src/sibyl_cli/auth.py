"""Auth-related CLI commands."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import typer

from sibyl_cli.auth_store import (
    clear_tokens,
    normalize_api_url,
    set_tokens,
)
from sibyl_cli.client import SibylClient, SibylClientError, get_client
from sibyl_cli.common import error, info, print_json, run_async, success

app = typer.Typer(help="Authentication and credentials")


def _issuer_url_from_api_url(api_url: str) -> str:
    parts = urlsplit(api_url)
    path = parts.path.rstrip("/")
    path = path.removesuffix("/api")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _pkce_s256(code_verifier: str) -> str:
    sha256 = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(sha256).decode("utf-8").rstrip("=")


def _compute_api_url(server: str | None) -> str:
    if server and server.strip():
        raw = server.strip().rstrip("/")
        if raw.endswith("/api"):
            return normalize_api_url(raw)
        return normalize_api_url(raw + "/api")

    env_api_url = os.environ.get("SIBYL_API_URL", "").strip()
    if env_api_url:
        return normalize_api_url(env_api_url)

    return normalize_api_url(SibylClient().base_url)


def _load_oauth_metadata(*, issuer_url: str, insecure: bool = False) -> dict:
    import httpx

    resp = httpx.get(
        issuer_url.rstrip("/") + "/.well-known/oauth-authorization-server",
        timeout=10,
        verify=not insecure,
    )
    resp.raise_for_status()
    return resp.json()


def _start_callback_server() -> tuple[HTTPServer, str, threading.Event, dict[str, str]]:
    result: dict[str, str] = {}
    done = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            _ = format, args  # Suppress logging

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [""])[0]
            if code:
                result["code"] = code
            if state:
                result["state"] = state

            body = (
                "Sibyl auth complete. You can close this tab and return to your terminal."
                if code
                else "Missing code. You can close this tab and try again."
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            done.set()

    httpd = HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = int(httpd.server_address[1])
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, redirect_uri, done, result


def _register_oauth_client(
    *,
    registration_endpoint: str,
    redirect_uri: str,
    insecure: bool = False,
) -> tuple[str, str]:
    import httpx

    reg_payload = {
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "mcp",
        "client_name": "sibyl-cli",
    }

    reg_resp = httpx.post(registration_endpoint, json=reg_payload, timeout=10, verify=not insecure)
    if reg_resp.status_code >= 400:
        reg_payload["token_endpoint_auth_method"] = "client_secret_post"
        reg_resp = httpx.post(
            registration_endpoint, json=reg_payload, timeout=10, verify=not insecure
        )
    reg_resp.raise_for_status()

    reg = reg_resp.json()
    client_id = str(reg.get("client_id", "")).strip()
    client_secret = str(reg.get("client_secret", "")).strip()
    if not client_id:
        raise ValueError("Client registration response missing client_id")
    return client_id, client_secret


def _exchange_oauth_code(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    code_verifier: str,
    resource: str,
    insecure: bool = False,
) -> dict:
    import httpx

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "resource": resource,
    }
    if client_secret:
        data["client_secret"] = client_secret

    token_resp = httpx.post(token_endpoint, data=data, timeout=15, verify=not insecure)
    token_resp.raise_for_status()
    return token_resp.json()


def _persist_tokens(
    *,
    api_url: str,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> None:
    """Persist access token and optionally refresh token for the server."""
    set_tokens(api_url, access_token, refresh_token=refresh_token, expires_in=expires_in)


class _DeviceLoginError(RuntimeError):
    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload


def _start_device_flow(*, api_url: str, insecure: bool = False) -> tuple[str, str, str, int, int]:
    import httpx

    start_url = api_url.rstrip("/") + "/auth/device"
    resp = httpx.post(
        start_url,
        json={"client_name": "sibyl-cli", "scope": "mcp", "interval": 5, "expires_in": 600},
        timeout=10,
        verify=not insecure,
    )
    resp.raise_for_status()
    start = resp.json()

    device_code = str(start.get("device_code", "")).strip()
    user_code = str(start.get("user_code", "")).strip()
    verify = (
        str(start.get("verification_uri_complete", "")).strip()
        or str(start.get("verification_uri", "")).strip()
    )
    interval = int(start.get("interval") or 5)
    expires_in = int(start.get("expires_in") or 600)

    if not device_code or not user_code or not verify:
        raise _DeviceLoginError("Server returned an invalid device auth payload", start)

    return device_code, user_code, verify, interval, expires_in


def _poll_device_token(
    *,
    token_url: str,
    device_code: str,
    interval: int,
    deadline: float,
    insecure: bool = False,
) -> dict:
    import httpx

    poll_interval = max(1, interval)
    while time.monotonic() < deadline:
        token_resp = httpx.post(
            token_url,
            data={
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15,
            verify=not insecure,
        )
        data = token_resp.json()

        if token_resp.status_code < 400 and isinstance(data, dict) and data.get("access_token"):
            return data

        err = str(data.get("error", "")).strip() if isinstance(data, dict) else ""
        if err in {"authorization_pending", ""}:
            time.sleep(poll_interval)
            continue
        if err == "slow_down":
            poll_interval = min(60, poll_interval + 2)
            time.sleep(poll_interval)
            continue

        raise _DeviceLoginError(
            f"Device login failed: {err or 'unknown'}", data if isinstance(data, dict) else None
        )

    raise TimeoutError("Timed out waiting for approval")


class _OAuthLoginError(RuntimeError):
    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload


def _oauth_pkce_login(
    *, api_url: str, no_browser: bool, timeout_seconds: int, insecure: bool = False
) -> tuple[str, str, str]:
    issuer_url = _issuer_url_from_api_url(api_url)
    resource = issuer_url.rstrip("/") + "/mcp"

    meta = _load_oauth_metadata(issuer_url=issuer_url, insecure=insecure)
    authorization_endpoint = str(meta.get("authorization_endpoint", "")).strip()
    token_endpoint = str(meta.get("token_endpoint", "")).strip()
    registration_endpoint = str(meta.get("registration_endpoint", "")).strip()
    if not authorization_endpoint or not token_endpoint or not registration_endpoint:
        raise _OAuthLoginError(
            "OAuth metadata missing authorization/token/registration endpoints", meta
        )

    httpd, redirect_uri, done, result = _start_callback_server()
    try:
        client_id, client_secret = _register_oauth_client(
            registration_endpoint=registration_endpoint,
            redirect_uri=redirect_uri,
            insecure=insecure,
        )
    except Exception as e:
        httpd.shutdown()
        raise _OAuthLoginError(f"OAuth setup failed: {e}") from e

    state = secrets.token_urlsafe(16)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _pkce_s256(code_verifier)

    auth_url = (
        authorization_endpoint
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": state,
                "scope": "mcp",
                "resource": resource,
            }
        )
    )

    if no_browser:
        print(auth_url)
    else:
        webbrowser.open(auth_url, new=1, autoraise=True)

    if not done.wait(timeout=max(10, timeout_seconds)):
        httpd.shutdown()
        raise TimeoutError("Timed out waiting for browser login")

    httpd.shutdown()
    code = result.get("code", "").strip()
    returned_state = result.get("state", "").strip()
    if not code or returned_state != state:
        raise _OAuthLoginError("OAuth callback missing code or state mismatch")

    tok = _exchange_oauth_code(
        token_endpoint=token_endpoint,
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        code_verifier=code_verifier,
        resource=resource,
        insecure=insecure,
    )

    access_token = str(tok.get("access_token", "")).strip()
    refresh_token = str(tok.get("refresh_token", "")).strip()
    expires_in = tok.get("expires_in")
    if not access_token:
        raise _OAuthLoginError("Token response missing access_token", tok)

    # Store tokens and OAuth metadata
    from sibyl_cli.auth_store import write_server_credentials

    set_tokens(
        api_url,
        access_token,
        refresh_token=refresh_token or None,
        expires_in=int(expires_in) if expires_in else None,
    )
    # Also store OAuth metadata for potential token refresh
    write_server_credentials(
        api_url,
        {
            "oauth_client_id": client_id,
            "oauth_client_secret": client_secret,
            "issuer_url": issuer_url,
        },
    )
    return access_token, refresh_token, issuer_url


def _local_password_login(*, api_url: str, email: str, password: str) -> dict:
    client = SibylClient(base_url=api_url, auth_token=None)

    @run_async
    async def _run() -> dict:
        try:
            return await client.local_login(email=email, password=password)
        finally:
            await client.close()

    return _run()


def _login_via_device_flow(
    *, api_url: str, no_browser: bool, timeout_seconds: int, insecure: bool = False
) -> dict:
    """Execute device authorization flow. Returns token response dict."""
    token_url = api_url.rstrip("/") + "/auth/device/token"
    device_code, user_code, verify, interval, expires_in = _start_device_flow(
        api_url=api_url, insecure=insecure
    )

    info(f"User code: {user_code}")
    if no_browser:
        print(verify)
    else:
        webbrowser.open(verify, new=1, autoraise=True)
        info(f"Opened browser to approve: {verify}")

    deadline = time.monotonic() + max(30, timeout_seconds, expires_in)
    tok = _poll_device_token(
        token_url=token_url,
        device_code=device_code,
        interval=interval,
        deadline=deadline,
        insecure=insecure,
    )
    access_token = str(tok.get("access_token", "")).strip()
    if not access_token:
        raise _DeviceLoginError("Token response missing access_token", tok)
    return tok


def _login_via_oauth(
    *, api_url: str, no_browser: bool, timeout_seconds: int, insecure: bool = False
) -> dict:
    """Execute OAuth PKCE flow. Returns token response dict."""
    access_token, refresh_token, _issuer = _oauth_pkce_login(
        api_url=api_url,
        no_browser=no_browser,
        timeout_seconds=timeout_seconds,
        insecure=insecure,
    )
    return {"access_token": access_token, "refresh_token": refresh_token}


def _login_via_local_password(*, api_url: str, email: str, password: str) -> dict:
    """Execute local password login. Returns token response dict."""
    result = _local_password_login(api_url=api_url, email=email, password=password)
    token = str(result.get("access_token", "")).strip()
    if not token:
        raise _OAuthLoginError("Local login response missing access_token", result)
    return result


def _login_auto(
    *,
    api_url: str,
    no_browser: bool,
    timeout_seconds: int,
    email: str | None,
    password: str | None,
    insecure: bool = False,
) -> None:
    """Single login entrypoint.

    Preference order:
    1) Device authorization flow (best for remote/headless)
    2) OAuth PKCE (FastMCP auth server)
    3) Local password login (only if email/password provided)
    """
    import httpx

    # 1) Device flow (preferred)
    try:
        tok = _login_via_device_flow(
            api_url=api_url,
            no_browser=no_browser,
            timeout_seconds=timeout_seconds,
            insecure=insecure,
        )
        _persist_tokens(
            api_url=api_url,
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token"),
            expires_in=tok.get("expires_in"),
        )
        success(f"Login complete (saved credentials for {api_url})")
        return
    except TimeoutError:
        error("Timed out waiting for approval")
        return
    except httpx.HTTPStatusError as e:
        # Not supported on server -> fall through to OAuth.
        if e.response.status_code not in {404, 405}:
            error(f"Device login failed: {e}")
            return
    except _DeviceLoginError as e:
        error(str(e))
        if e.payload is not None:
            print_json(e.payload)
        return
    except Exception as e:
        # Best-effort fall-through to OAuth when device flow isn't available.
        info(f"Device login unavailable ({type(e).__name__}); trying OAuth login.")

    # 2) OAuth PKCE
    try:
        tok = _login_via_oauth(
            api_url=api_url,
            no_browser=no_browser,
            timeout_seconds=timeout_seconds,
            insecure=insecure,
        )
        _persist_tokens(
            api_url=api_url,
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token"),
            expires_in=tok.get("expires_in"),
        )
        success(f"Login complete (saved credentials for {api_url})")
        return
    except TimeoutError:
        error("Timed out waiting for browser login")
        return
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in {404, 405}:
            error(f"OAuth login failed: {e}")
            return
    except _OAuthLoginError as e:
        # OAuth isn't available / misconfigured -> try local.
        if e.payload is not None:
            info("OAuth not available; falling back to local login if credentials provided.")
        else:
            info("OAuth not available; falling back to local login if credentials provided.")
    except Exception as e:
        info(
            f"OAuth login unavailable ({type(e).__name__}); falling back to local login if credentials provided."
        )

    # 3) Local password (optional)
    if not email or not password:
        error(
            "No supported login methods detected for this server (need device/oauth, or provide --email/--password)."
        )
        return

    try:
        tok = _login_via_local_password(api_url=api_url, email=email, password=password)
    except SibylClientError as e:
        error(str(e))
        return
    except _OAuthLoginError as e:
        error(str(e))
        if e.payload is not None:
            print_json(e.payload)
        return

    _persist_tokens(
        api_url=api_url,
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token"),
        expires_in=tok.get("expires_in"),
    )
    success(f"Login complete (saved credentials for {api_url})")
    return


@app.command("status")
def status_cmd() -> None:
    """Show auth status for the current context."""
    from sibyl_cli.auth_store import get_access_token

    api_url = _compute_api_url(None)
    token = get_access_token(api_url)
    if token:
        success(f"Auth token found (server: {api_url})")
    else:
        error("No auth token found (set one with: sibyl auth set-token <token>)")


@app.command("set-token")
def set_token_cmd(
    token: str,
    server: str | None = typer.Option(
        None,
        "--server",
        "-s",
        help="Server URL to set token for (defaults to active context)",
    ),
) -> None:
    """Set an auth token for a server."""
    api_url = _compute_api_url(server)
    set_tokens(api_url, token.strip())
    success(f"Auth token saved for {api_url}")


@app.command("clear-token")
def clear_token_cmd(
    server: str | None = typer.Option(
        None,
        "--server",
        "-s",
        help="Server URL to clear tokens for (defaults to active context)",
    ),
    all_servers: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Clear tokens for ALL servers",
    ),
) -> None:
    """Clear auth tokens for a server."""
    from sibyl_cli.auth_store import clear_all_tokens

    if all_servers:
        clear_all_tokens()
        success("All auth tokens cleared")
    else:
        api_url = _compute_api_url(server)
        clear_tokens(api_url)
        success(f"Auth tokens cleared for {api_url}")


@app.command("login")
def login_cmd(
    url: str = typer.Argument(
        "",
        help="Server URL (e.g. https://sibyl.example.com). If omitted, uses active context or default.",
    ),
    server: str | None = typer.Option(
        None,
        "--server",
        "-s",
        help="Server base URL (alias for positional URL)",
    ),
    context: str | None = typer.Option(
        None,
        "--context",
        "-c",
        help="Create/update a named context for this server",
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print URL instead of opening browser"
    ),
    timeout_seconds: int = typer.Option(180, "--timeout", help="Time to wait for approval/auth"),
    email: str | None = typer.Option(
        None, "--email", "-e", help="Email for local login (method=local)"
    ),
    password: str | None = typer.Option(
        None, "--password", "-p", help="Password for local login (method=local)"
    ),
    insecure: bool = typer.Option(
        False,
        "--insecure",
        "-k",
        help="Disable SSL certificate verification (for self-signed certs)",
    ),
) -> None:
    """Login to a Sibyl server and save credentials.

    Examples:
        sibyl auth login                           # Login to active context or default
        sibyl auth login https://sibyl.example.com # Login to specific server
        sibyl auth login https://prod.com -c prod  # Login and create 'prod' context
    """
    # Positional URL takes precedence over --server option
    effective_server = url.strip() if url.strip() else server
    api_url = _compute_api_url(effective_server)

    # Perform login
    _login_auto(
        api_url=api_url,
        no_browser=no_browser,
        timeout_seconds=timeout_seconds,
        email=email,
        password=password,
        insecure=insecure,
    )

    # Create/update context if requested
    if context:
        from sibyl_cli.client import clear_client_cache
        from sibyl_cli.config_store import (
            create_context,
            get_context,
            update_context,
        )

        # Extract base server URL from api_url (remove /api suffix)
        parts = urlsplit(api_url)
        path = parts.path.rstrip("/").removesuffix("/api")
        server_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))

        existing = get_context(context)
        if existing:
            update_context(context, server_url=server_url, insecure=insecure)
            info(f"Updated context '{context}' with server {server_url}")
        else:
            create_context(name=context, server_url=server_url, set_active=True, insecure=insecure)
            success(f"Created context '{context}' and set as active")

        clear_client_cache()


@app.command("local-signup")
def local_signup_cmd(
    email: str = typer.Option(..., "--email", "-e", help="Email address"),
    password: str = typer.Option(..., "--password", "-p", help="Password (min 8 chars)"),
    name: str = typer.Option(..., "--name", "-n", help="Display name"),
) -> None:
    """Create a local user and save the returned access token."""
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.local_signup(email=email, password=password, name=name)

    try:
        result = _run()
        token = str(result.get("access_token", "")).strip()
        if token:
            _persist_tokens(
                api_url=_compute_api_url(None),
                access_token=token,
                refresh_token=result.get("refresh_token"),
                expires_in=result.get("expires_in"),
            )
            success("Auth tokens saved to ~/.sibyl/auth.json")
        print_json(result)
    except SibylClientError as e:
        error(str(e))


api_key_app = typer.Typer(help="API key management")
app.add_typer(api_key_app, name="api-key")


@api_key_app.command("list")
def api_key_list() -> None:
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.list_api_keys()

    try:
        result = _run()
        print_json(result)
    except SibylClientError as e:
        error(str(e))


@api_key_app.command("create")
def api_key_create(
    name: str = typer.Option(..., "--name", "-n", help="Display name for this key"),
    live: bool = typer.Option(True, "--live/--test", help="Use sk_live_ (default) or sk_test_"),
    scopes: str = typer.Option(
        "mcp",
        "--scopes",
        help="Comma-separated scopes (default: mcp)",
    ),
    expires_days: int | None = typer.Option(
        None,
        "--expires-days",
        help="Optional expiry in days (1-365)",
        min=1,
        max=365,
    ),
) -> None:
    client = get_client()

    @run_async
    async def _run() -> dict:
        scope_list = [s.strip() for s in (scopes or "").split(",") if s.strip()]
        return await client.create_api_key(
            name=name,
            live=live,
            scopes=scope_list,
            expires_days=expires_days,
        )

    try:
        result = _run()
        print_json(result)
    except SibylClientError as e:
        error(str(e))


@api_key_app.command("revoke")
def api_key_revoke(api_key_id: str) -> None:
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.revoke_api_key(api_key_id)

    try:
        result = _run()
        print_json(result)
    except SibylClientError as e:
        error(str(e))
