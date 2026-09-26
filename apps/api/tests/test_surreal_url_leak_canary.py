"""No path that touches the SurrealDB URL may print its secret parts.

Each URL below carries two canaries: one in the password and one in the path
(or in the port text, where the URL is malformed). Every path that formats,
logs, returns, or prints the configured URL is driven with every URL, and the
canaries must never appear, in any case. A new leak fails here rather than in
a review round.

The CLI's `migrate` command lives in a package the API suite does not
install, so its half of the canary runs in
apps/cli/tests/test_server_url_independence.py with the same URL shapes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import subprocess
import traceback
from collections.abc import AsyncIterator
from importlib import import_module
from pathlib import Path
from shutil import which
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from structlog.testing import capture_logs

from sibyl.api.routes import admin as admin_routes
from sibyl_core.backends.surreal.connection import SurrealConnectTimeout
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.url_schemes import redact_surreal_url
from sibyl_core.config import core_config

pytest.importorskip("surrealdb")

REPO_ROOT = Path(__file__).resolve().parents[3]
PASSWORD_CANARY = "PwCanary91"
PATH_CANARY = "PathCanary42"
TOKEN_CANARY = "TokenCanary91"
CANARIES = (PASSWORD_CANARY, PATH_CANARY, TOKEN_CANARY)
# urlsplit rejects it under NFKC normalization, quoting the netloc.
FULLWIDTH_SOLIDUS = "\N{FULLWIDTH SOLIDUS}"


def _urls(tmp_path: Path) -> dict[str, str]:
    # A regular file where the store's directory should be, so opening the
    # embedded store fails with an error that quotes its path.
    blocker = tmp_path / PATH_CANARY
    blocker.write_text("not a directory")
    return {
        "userinfo-and-path": (
            f"wss://admin:{PASSWORD_CANARY}@127.0.0.1:9/private/{PATH_CANARY}/rpc"
            f"?token={TOKEN_CANARY}#{PATH_CANARY}"
        ),
        # Without a fragment, which the WebSocket stack rejects before it
        # ever connects, so this one reaches a refused TCP connect.
        "remote-no-fragment": (
            f"ws://admin:{PASSWORD_CANARY}@127.0.0.1:9/private/{PATH_CANARY}/rpc"
            f"?token={TOKEN_CANARY}"
        ),
        "scheme-less": f"admin:{PASSWORD_CANARY}@host:8000/{PATH_CANARY}/rpc?next=http://x",
        "malformed-host": f"ws:///admin:{PASSWORD_CANARY}@host:8000/{PATH_CANARY}/rpc",
        "fullwidth-slash": f"ws://admin:{PASSWORD_CANARY}@host{FULLWIDTH_SOLIDUS}{PATH_CANARY}/rpc",
        "port-text": f"ws://admin:{PASSWORD_CANARY}@host:{PATH_CANARY}/rpc",
        "embedded-path": f"surrealkv://{blocker}/sibyl",
    }


URL_KINDS = [
    "userinfo-and-path",
    "remote-no-fragment",
    "scheme-less",
    "malformed-host",
    "fullwidth-slash",
    "port-text",
    "embedded-path",
]


def _assert_no_canary(text: str, where: str) -> None:
    lowered = text.lower()
    for canary in CANARIES:
        assert canary.lower() not in lowered, f"{canary} leaked through {where}:\n{text}"


def _sibyl_logs(caplog: pytest.LogCaptureFixture) -> str:
    """Every stdlib log line Sibyl wrote, at any level.

    Third-party wire logs (websockets and aiohttp at DEBUG print each request
    line) are outside the client boundary this canary guards.
    """
    return "\n".join(
        f"{record.name}: {record.getMessage()}"
        for record in caplog.records
        if record.name.startswith("sibyl")
    )


def _assert_error_clean(
    error: BaseException, entries: list[dict[str, object]], logs: str, where: str
) -> None:
    _assert_no_canary(str(error), f"str() of {where}")
    _assert_no_canary(repr(error), f"repr() of {where}")
    # format_exception prints every chained cause and context too.
    _assert_no_canary("".join(traceback.format_exception(error)), f"traceback of {where}")
    _assert_no_canary(repr(entries), f"structlog output of {where}")
    _assert_no_canary(logs, f"stdlib log output of {where}")


@pytest.fixture
def url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    return _urls(tmp_path)[request.param]


@pytest.fixture(autouse=True)
def short_connect_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_config, "surreal_connect_timeout_seconds", 2.0)


@pytest.mark.parametrize("url", URL_KINDS, indirect=True)
def test_redaction_and_timeout_never_carry_the_canary(url: str) -> None:
    _assert_no_canary(redact_surreal_url(url), "redact_surreal_url")
    timeout = SurrealConnectTimeout(url=url, attempt=1, timeout_seconds=1.0)
    _assert_no_canary(str(timeout), "SurrealConnectTimeout")
    _assert_no_canary("".join(traceback.format_exception(timeout)), "timeout traceback")


@pytest.mark.parametrize("url", URL_KINDS, indirect=True)
async def test_a_failed_connect_never_carries_the_canary(url: str) -> None:
    client = DedicatedSurrealClient(
        url=url,
        username="root",
        password="root",
        namespace="org_canary",
        database="graph",
        pool_size=1,
    )
    with capture_logs() as entries:
        try:
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(client.execute_query("RETURN 1;"), timeout=20)
        finally:
            await client.close()

    _assert_no_canary(str(caught.value), "the connect error")
    _assert_no_canary(
        "".join(traceback.format_exception(caught.value)), "the connect error's traceback"
    )
    _assert_no_canary(repr(entries), "structlog connect and query logs")


@pytest.mark.parametrize("url", URL_KINDS, indirect=True)
def test_the_sibyld_service_check_never_prints_the_canary(monkeypatch, url: str) -> None:
    cli_main = import_module("sibyl.cli.main")

    cli_common = import_module("sibyl.cli.common")

    printed: list[str] = []
    monkeypatch.setattr(cli_main, "info", lambda message: printed.append(str(message)))
    for name in ("success", "error"):
        monkeypatch.setattr(cli_common, name, lambda message: printed.append(str(message)))
    monkeypatch.setattr(cli_main, "_tcp_service_running", lambda _host, _port: False)
    monkeypatch.setattr(
        cli_main.console, "print", lambda *args, **_kw: printed.append(" ".join(map(str, args)))
    )
    settings = SimpleNamespace(
        resolved_surreal_url=url,
        auth_store="surreal",
        resolved_coordination_backend="local",
        redis_host="127.0.0.1",
        redis_port=6381,
    )

    cli_main._check_surreal_services(settings)

    assert printed
    _assert_no_canary("\n".join(printed), "the sibyld service check")


@pytest.mark.parametrize("url", URL_KINDS, indirect=True)
async def test_admin_health_never_returns_the_canary(monkeypatch, url: str) -> None:
    from sibyl_core.services import graph_client

    monkeypatch.setattr(core_config, "surreal_url", url)
    monkeypatch.setattr(core_config, "surreal_data_dir", "")
    monkeypatch.setattr(graph_client, "_clients", graph_client.OrderedDict())
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

    try:
        response = await asyncio.wait_for(admin_routes.health(org=org), timeout=20)
    finally:
        await graph_client.close_graph_clients()

    _assert_no_canary(response.model_dump_json(), "/admin/health")


class _FailingClient:
    """An HTTP client whose errors quote the request URL, as httpx's can."""

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FailingClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(self, url: str, auth: object = None) -> object:
        raise httpx.ConnectError(f"All connection attempts failed for {url} as {auth}")


@pytest.mark.parametrize("url", URL_KINDS, indirect=True)
async def test_debug_status_payload_never_carries_the_canary(monkeypatch, url: str) -> None:
    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", _FailingClient)
    monkeypatch.setattr(
        admin_routes,
        "settings",
        SimpleNamespace(
            resolved_surreal_url=url,
            surreal_username="",
            surreal_password=SimpleNamespace(get_secret_value=lambda: ""),
        ),
    )
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    with (
        patch(
            "sibyl_core.tools.core.get_health",
            AsyncMock(return_value={"status": "healthy", "graph_connected": True}),
        ),
        patch("sibyl.api.routes.admin.get_graph_stats_payload", AsyncMock(return_value={})),
        patch(
            "sibyl.api.routes.admin.get_coordination_health",
            AsyncMock(return_value={"backend": "local", "status": "healthy"}),
        ),
        patch(
            "sibyl_core.logging.LogBuffer.get",
            return_value=SimpleNamespace(tail=lambda **_: []),
        ),
    ):
        # The handler behind `sibyl debug status --json`.
        response = await admin_routes.dev_status(org=org)

    _assert_no_canary(response.model_dump_json(), "/admin/dev-status")
    observability = await admin_routes.get_surreal_observability_status()
    _assert_no_canary(json.dumps(observability), "the Surreal observability payload")


@pytest.mark.parametrize("url", URL_KINDS, indirect=True)
@pytest.mark.parametrize("print_env", ["--print-env", None])
def test_the_dev_script_never_prints_the_canary(
    tmp_path: Path, url: str, print_env: str | None
) -> None:
    bash = which("bash")
    assert bash is not None
    env = {
        **os.environ,
        "SIBYL_STORE": "surreal",
        "SIBYL_AUTH_STORE": "surreal",
        "SIBYL_COORDINATION_BACKEND": "local",
        "SIBYL_SURREAL_URL": url,
        "SIBYL_DEV_API_COMMAND": "true",
        "SIBYL_DEV_WEB_COMMAND": "true",
    }
    env.pop("SIBYL_SURREAL_DATA_DIR", None)
    main_call = f"main {print_env}" if print_env else "main"
    script = f"""
source tools/dev/run-surreal-dev.sh
pid_file={shlex.quote(str(tmp_path / "processes.pid"))}
sleep() {{ :; }}
launch_command() {{ child_pids+=("99999"); }}
wait_for_api_ready() {{ return 0; }}
wait_for_commands() {{ child_pids=(); return 0; }}
cleanup() {{ exit "${{1:-0}}"; }}
{main_call}
"""
    result = subprocess.run(  # noqa: S603
        [bash, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Surreal URL" in result.stdout or "SIBYL_SURREAL_URL=" in result.stdout
    _assert_no_canary(result.stdout + result.stderr, "tools/dev/run-surreal-dev.sh")


# --- Real transports -------------------------------------------------------
#
# The cases above mostly fail before a socket opens. These drive the SDK's
# actual HTTP and WebSocket stacks against local servers, so errors raised
# after a successful handshake, and URLs the HTTP stack normalizes, are
# covered too. Each fake server echoes the request path, as a real proxy or
# server may, so a leak through server content is caught as well.

_HANDSHAKE_METHODS = {"signin", "use", "authenticate"}
_TRANSPORT_PATHS = {
    "plain-path": f"/private/{PATH_CANARY}/rpc?token={TOKEN_CANARY}",
    # aiohttp decodes %50 to P, so the error quotes "PathCanary42".
    "encoded-path": f"/%50ath{PATH_CANARY[4:]}/rpc?token={TOKEN_CANARY}",
    # The same with nothing else secret in the URL, so only the decoded
    # spelling of the path can catch it.
    "encoded-path-only": f"/%50ath{PATH_CANARY[4:]}/rpc",
}


def _cbor_reply(message: dict[str, object]) -> bytes:
    from surrealdb.data.cbor import encode

    result = "fake-token" if message.get("method") == "signin" else None
    return encode({"id": message["id"], "result": result})


@contextlib.asynccontextmanager
async def _surreal_http_server(fail: str) -> AsyncIterator[int]:
    """A SurrealDB HTTP endpoint that answers the handshake, then fails.

    ``fail`` is "query" (503 on the first query) or "handshake" (503 always).
    """
    from aiohttp import web
    from surrealdb.data.cbor import decode

    async def rpc(request: web.Request) -> web.Response:
        message = decode(await request.read())
        if fail == "handshake" or message.get("method") not in _HANDSHAKE_METHODS:
            return web.Response(status=503, text=f"unavailable: {request.path_qs}")
        return web.Response(body=_cbor_reply(message), content_type="application/cbor")

    app = web.Application()
    app.router.add_post("/{tail:.*}", rpc)
    # No access log: it is the fake server's own record of the path it was sent.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    finally:
        await runner.cleanup()


@contextlib.asynccontextmanager
async def _surreal_ws_server(behaviour: str) -> AsyncIterator[int]:
    """A SurrealDB WebSocket endpoint that accepts, answers the handshake, then fails.

    "error-frame" answers the query with an RPC error quoting the request path;
    "close" closes the socket with the path as its reason; "reject" refuses
    the upgrade with a 503 whose body quotes the path.
    """
    from surrealdb.data.cbor import decode, encode
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def process_request(connection, request):
        if behaviour == "reject":
            body = f"unavailable: {request.path}".encode()
            return Response(503, "Service Unavailable", Headers(), body)
        return None

    async def handler(socket) -> None:
        path = socket.request.path
        async for raw in socket:
            message = decode(raw)
            if message.get("method") in _HANDSHAKE_METHODS:
                await socket.send(_cbor_reply(message))
                continue
            if behaviour == "error-frame":
                error = {"code": -32000, "message": f"cannot serve {path}"}
                await socket.send(encode({"id": message["id"], "error": error}))
            else:
                await socket.close(1011, f"closing {path}"[:120])
                return

    async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        yield server.sockets[0].getsockname()[1]


async def _drive_client(
    url: str, caplog: pytest.LogCaptureFixture, *, sign_in: bool = True
) -> tuple[BaseException, list]:
    # Without a sign-in the HTTP stack authenticates with the URL's userinfo;
    # with one it refuses to combine the two.
    client = DedicatedSurrealClient(
        url=url,
        username="root" if sign_in else "",
        password="root" if sign_in else "",
        namespace="org_canary",
        database="graph",
        pool_size=1,
    )
    caplog.set_level(logging.DEBUG)
    with capture_logs() as entries:
        try:
            with pytest.raises(Exception) as caught:
                await asyncio.wait_for(client.execute_query("SELECT * FROM entity;"), 20)
        finally:
            await client.close()
    return caught.value, entries


@pytest.mark.parametrize("path", _TRANSPORT_PATHS.values(), ids=_TRANSPORT_PATHS.keys())
@pytest.mark.parametrize("fail", ["query", "handshake"])
async def test_http_transport_errors_never_carry_the_canary(caplog, fail: str, path: str) -> None:
    async with _surreal_http_server(fail) as port:
        url = f"http://admin:{PASSWORD_CANARY}@127.0.0.1:{port}{path}"
        error, entries = await _drive_client(url, caplog, sign_in=False)

    # The real HTTP stack failed; the boundary replaced its error.
    assert "ClientResponseError" in str(error)
    _assert_error_clean(error, entries, _sibyl_logs(caplog), f"an HTTP {fail} failure")


@pytest.mark.parametrize("behaviour", ["error-frame", "close", "reject"])
async def test_websocket_transport_errors_never_carry_the_canary(caplog, behaviour: str) -> None:
    async with _surreal_ws_server(behaviour) as port:
        url = (
            f"ws://admin:{PASSWORD_CANARY}@127.0.0.1:{port}/private/{PATH_CANARY}/rpc"
            f"?token={TOKEN_CANARY}"
        )
        error, entries = await _drive_client(url, caplog)

    _assert_error_clean(error, entries, _sibyl_logs(caplog), f"a WebSocket {behaviour} failure")


async def test_the_real_timeout_branch_never_chains_the_canary(monkeypatch, caplog) -> None:
    import aiohttp

    async def timed_out(self, method: str, url: object, **_kwargs: object) -> object:
        # aiohttp's own connect timeout quotes the URL it was requesting.
        raise aiohttp.ConnectionTimeoutError(f"Connection timeout to host {url}")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", timed_out)
    url = (
        f"http://admin:{PASSWORD_CANARY}@127.0.0.1:9/private/{PATH_CANARY}/rpc?token={TOKEN_CANARY}"
    )
    error, entries = await _drive_client(url, caplog)

    assert isinstance(error, SurrealConnectTimeout)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_error_clean(error, entries, _sibyl_logs(caplog), "the connect timeout branch")


async def test_a_query_time_error_through_admin_health_never_carries_the_canary(
    monkeypatch, caplog
) -> None:
    from sibyl_core.services import graph_client

    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    async with _surreal_http_server("query") as port:
        url = f"http://admin:{PASSWORD_CANARY}@127.0.0.1:{port}{_TRANSPORT_PATHS['encoded-path']}"
        monkeypatch.setattr(core_config, "surreal_url", url)
        monkeypatch.setattr(core_config, "surreal_data_dir", "")
        monkeypatch.setattr(graph_client, "_clients", graph_client.OrderedDict())
        monkeypatch.setattr(graph_client, "_prepared_groups", set())
        caplog.set_level(logging.DEBUG)
        with capture_logs() as entries:
            try:
                response = await asyncio.wait_for(admin_routes.health(org=org), timeout=30)
            finally:
                await graph_client.close_graph_clients()

    body = response.model_dump_json()
    # The handshake succeeded and a query then failed with a 503.
    assert response.status == "unhealthy"
    assert "ClientResponseError" in body
    _assert_no_canary(body, "the /admin/health response body")
    _assert_no_canary(repr(entries), "structlog output of /admin/health")
    _assert_no_canary(_sibyl_logs(caplog), "stdlib log output of /admin/health")
