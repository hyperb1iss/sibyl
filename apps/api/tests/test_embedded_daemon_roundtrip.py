"""The embedded daemon serves a fresh signup, its session reads, and a restart.

This drives `sibyld serve --embedded` as a real process on a scratch HOME, the
way `install.sh --daemon` and `sibyl start` run it. The daemon opens its auth,
content, and graph clients on one SurrealKV directory, which in-process tests
on memory:// never do: separate engines there corrupted each other's reads
(/auth/me answered 500 right after signup) and the graph never reached disk.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
def _embedded_daemon(home: Path, port: int, log_path: Path) -> Iterator[str]:
    base_url = f"http://127.0.0.1:{port}/api"
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
            env=_daemon_env(home),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while True:
            if process.poll() is not None:
                pytest.fail(f"embedded daemon exited early:\n{_log_tail(log_path)}")
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
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
    port = _free_port()

    with (
        _embedded_daemon(home, port, log_path) as base_url,
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
        _embedded_daemon(home, port, log_path) as base_url,
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
