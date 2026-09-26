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
import json
import os
import shlex
import subprocess
import traceback
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
            f"?token={PASSWORD_CANARY}#{PATH_CANARY}"
        ),
        "scheme-less": f"admin:{PASSWORD_CANARY}@host:8000/{PATH_CANARY}/rpc?next=http://x",
        "malformed-host": f"ws:///admin:{PASSWORD_CANARY}@host:8000/{PATH_CANARY}/rpc",
        "fullwidth-slash": f"ws://admin:{PASSWORD_CANARY}@host{FULLWIDTH_SOLIDUS}{PATH_CANARY}/rpc",
        "port-text": f"ws://admin:{PASSWORD_CANARY}@host:{PATH_CANARY}/rpc",
        "embedded-path": f"surrealkv://{blocker}/sibyl",
    }


URL_KINDS = [
    "userinfo-and-path",
    "scheme-less",
    "malformed-host",
    "fullwidth-slash",
    "port-text",
    "embedded-path",
]


def _assert_no_canary(text: str, where: str) -> None:
    lowered = text.lower()
    for canary in (PASSWORD_CANARY, PATH_CANARY):
        assert canary.lower() not in lowered, f"{canary} leaked through {where}:\n{text}"


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
