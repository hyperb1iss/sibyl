"""The embedded daemon serves a fresh signup, its session reads, and a restart.

This drives `sibyld serve --embedded` as a real process on a scratch HOME, the
way `install.sh --daemon` and `sibyl start` run it. The daemon opens its auth,
content, and graph clients on one SurrealKV directory, which in-process tests
on memory:// never do: separate engines there corrupted each other's reads
(/auth/me answered 500 right after signup) and the graph never reached disk.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

import httpx2 as httpx
import pytest

pytest.importorskip("surrealdb")

_STARTUP_TIMEOUT_SECONDS = 90.0
_EMAIL = "nova@example.com"
_PASSWORD = "correct-horse-battery-staple-42"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _port_is_answering(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _daemon_identity(response: httpx.Response) -> object:
    try:
        payload = response.json()
    except ValueError:
        return None
    runtime = payload.get("runtime") if isinstance(payload, dict) else None
    return runtime.get("commit") if isinstance(runtime, dict) else None


def _daemon_env(home: Path) -> dict[str, str]:
    # Built from nothing so provider keys and SIBYL_* settings from the
    # calling shell cannot reach the daemon.
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SIBYL_ENVIRONMENT": "development",
        "SIBYL_JWT_SECRET": "embedded-roundtrip-secret-0123456789abcdef",
        "SIBYL_EMBEDDED_DATA_DIR": str(home / "surreal"),
    }
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


@contextmanager
def _embedded_daemon(home: Path, log_path: Path) -> Iterator[str]:
    """Start a daemon and yield its API URL once it, and only it, answers.

    The port is picked free but could be claimed by another process before the
    daemon binds it, so a healthy answer alone does not prove which process
    gave it. Each daemon echoes a nonce through `SIBYL_GIT_COMMIT` in its
    /health runtime block, and anything else on the port fails the test.
    """
    port = _free_port()
    if _port_is_answering(port):
        pytest.fail(f"port {port} was reported free but something is already listening on it")
    base_url = f"http://127.0.0.1:{port}/api"
    nonce = f"embedded-roundtrip-{uuid4().hex}"
    env = {**_daemon_env(home), "SIBYL_GIT_COMMIT": nonce}
    with log_path.open("ab") as log:
        process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-c",
                "from sibyl.cli import main; main()",
                "serve",
                "--embedded",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while True:
            if process.poll() is not None:
                pytest.fail(
                    f"embedded daemon exited early (code {process.returncode}); if the log "
                    f"shows 'address already in use', another process took port {port}:\n"
                    f"{_log_tail(log_path)}"
                )
            try:
                response = httpx.get(f"{base_url}/health", timeout=2)
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code == 200:
                identity = _daemon_identity(response)
                if identity == nonce:
                    break
                pytest.fail(
                    f"port {port} is served by another process (runtime.commit={identity!r}), "
                    "not the daemon this test started"
                )
            if time.monotonic() > deadline:
                pytest.fail(f"embedded daemon never became healthy:\n{_log_tail(log_path)}")
            time.sleep(0.25)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _log_tail(log_path: Path, lines: int = 60) -> str:
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-lines:])


def _expect(response: httpx.Response, status: int, log_path: Path) -> httpx.Response:
    assert response.status_code == status, (
        f"{response.request.method} {response.request.url} -> {response.status_code}: "
        f"{response.text[:400]}\n{_log_tail(log_path)}"
    )
    return response


def test_embedded_daemon_signup_session_reads_and_restart(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    log_path = tmp_path / "sibyld.log"

    with (
        _embedded_daemon(home, log_path) as base_url,
        httpx.Client(base_url=base_url, timeout=30) as client,
    ):
        _expect(
            client.post(
                "/auth/local/signup",
                json={"email": _EMAIL, "password": _PASSWORD, "name": "Nova"},
            ),
            201,
            log_path,
        )
        me = _expect(client.get("/auth/me"), 200, log_path).json()
        assert me["user"]["email"] == _EMAIL
        assert me["org_role"] == "owner"
        orgs = _expect(client.get("/orgs"), 200, log_path).json()
        assert orgs
        _expect(
            client.post(
                "/entities",
                params={"sync": "true"},
                json={
                    "name": "embedded roundtrip probe",
                    "content": "written before the daemon restarts",
                    "entity_type": "episode",
                },
            ),
            201,
            log_path,
        )

    with (
        _embedded_daemon(home, log_path) as base_url,
        httpx.Client(base_url=base_url, timeout=30) as client,
    ):
        _expect(
            client.post("/auth/local/login", json={"email": _EMAIL, "password": _PASSWORD}),
            200,
            log_path,
        )
        assert _expect(client.get("/auth/me"), 200, log_path).json()["user"]["email"] == _EMAIL
        listed = _expect(client.get("/entities", params={"limit": 50}), 200, log_path).json()
        names = [entity["name"] for entity in listed["entities"]]
        assert "embedded roundtrip probe" in names

    log_text = log_path.read_text(errors="replace")
    assert "Invalid revision" not in log_text
    assert "unhandled_exception" not in log_text


@contextmanager
def _impostor_health_server() -> Iterator[int]:
    """Answer /api/health with 200 the way some other daemon on the port would."""
    body = json.dumps({"status": "healthy", "runtime": {"commit": "someone-else"}}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def test_a_port_that_is_already_answering_fails_before_spawning(monkeypatch, tmp_path) -> None:
    with _impostor_health_server() as port:
        monkeypatch.setattr(sys.modules[__name__], "_free_port", lambda: port)
        with (
            pytest.raises(pytest.fail.Exception, match="already listening"),
            _embedded_daemon(tmp_path / "home", tmp_path / "sibyld.log"),
        ):
            pass


def test_a_healthy_answer_from_another_process_fails_loudly(monkeypatch, tmp_path) -> None:
    # The race the pre-check cannot close: the port is claimed after it is
    # probed. The impostor's 200 must not pass for the daemon's.
    (tmp_path / "home").mkdir()
    with _impostor_health_server() as port:
        monkeypatch.setattr(sys.modules[__name__], "_free_port", lambda: port)
        monkeypatch.setattr(sys.modules[__name__], "_port_is_answering", lambda _port: False)
        with (
            pytest.raises(pytest.fail.Exception, match="served by another process"),
            _embedded_daemon(tmp_path / "home", tmp_path / "sibyld.log"),
        ):
            pass
