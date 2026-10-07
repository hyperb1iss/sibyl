"""Owned native daemon and after-commit response loss for lifecycle acceptance."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx

from tests.conftest import CLIResult, CLIRunner

REPO_ROOT = Path(__file__).resolve().parents[4]
PYTHON = REPO_ROOT / ".venv/bin/python"


def isolated_env(home: Path) -> dict[str, str]:
    """Build child environments without inherited credentials or project state."""
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SIBYL_ENVIRONMENT": "development",
        "SIBYL_JWT_SECRET": "lifecycle-acceptance-secret-0123456789abcdef",
        "SIBYL_EMBEDDED_DATA_DIR": str(home / "surreal"),
        "SIBYL_AUTO_EXTRACT_ENTITIES": "false",
        # Missing provider credentials select the real no-embedding path.
        "SIBYL_GRAPH_EMBEDDING_PROVIDER": "openai",
    }


class OwnedCLI(CLIRunner):
    """Reuse the public runner while isolating every subprocess and its queue."""

    def __init__(self, runtime: LifecycleRuntime, token: str):
        super().__init__(auth_token=token)
        self.runtime = runtime

    def run(self, *args: str, timeout: float = 60) -> CLIResult:
        process = subprocess.Popen(
            [str(PYTHON), "-c", "from sibyl_cli.entrypoint import main; main()", *args],
            cwd=self.runtime.home,
            env={
                **isolated_env(self.runtime.home),
                "SIBYL_API_URL": self.runtime.proxy_url,
                "SIBYL_AUTH_TOKEN": self.auth_token,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        result = CLIResult(process.returncode, stdout, stderr)
        self.runtime.record(
            "cli",
            {
                "pid": process.pid,
                "args": args,
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
        )
        return result


class LifecycleRuntime:
    """Own one persistent native store, daemon, proxy, and isolated CLI HOME."""

    def __init__(self, directory: Path):
        assert PYTHON.is_file(), "Prepare the owned backend environment with moon run api:sync"
        self.directory = directory
        self.home = directory / "home"
        self.home.mkdir(parents=True)
        self.log_path = directory / "sibyld.log"
        self.events: list[dict] = []
        self.requests: list[dict] = []
        self.drop_path: str | None = None
        self.process: subprocess.Popen | None = None
        self.upstream_url = ""
        self.lock = threading.RLock()
        owner = self

        class Proxy(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self.forward()

            def do_POST(self) -> None:
                self.forward()

            def do_PATCH(self) -> None:
                self.forward()

            def forward(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"host", "connection", "content-length"}
                }
                with httpx.Client(timeout=60, trust_env=False) as client:
                    response = client.request(
                        self.command,
                        owner.upstream_url.removesuffix("/api") + self.path,
                        headers=headers,
                        content=body,
                    )
                payload = response.json()
                recorded_payload = (
                    {
                        key: value
                        for key, value in payload.items()
                        if key not in {"access_token", "refresh_token"}
                    }
                    if isinstance(payload, dict)
                    else payload
                )
                observation = {
                    "method": self.command,
                    "path": self.path,
                    "key": self.headers.get("Idempotency-Key"),
                    "status": response.status_code,
                    "response": recorded_payload,
                }
                with owner.lock:
                    owner.requests.append(observation)
                    drop = (
                        self.command == "POST"
                        and self.path == owner.drop_path
                        and response.is_success
                    )
                    if drop:
                        receipt = payload.get("mutation_receipt", {})
                        assert receipt.get("applied") is True, payload
                        assert receipt.get("idempotency_key") == observation["key"], payload
                        owner.drop_path = None
                        observation["ack_dropped_after_applied_receipt"] = True
                    owner.record("http", observation)
                if drop:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(response.status_code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            def log_message(self, *_args: object) -> None:
                return

        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        self.proxy.daemon_threads = True
        self.proxy_url = f"http://127.0.0.1:{self.proxy.server_port}/api"
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()

    def record(self, step: str, payload: dict) -> None:
        with self.lock:
            self.events.append({"step": step, **payload})
            (self.directory / "receipt.json").write_text(json.dumps(self.events, indent=2))

    def start(self) -> None:
        assert self.process is None
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{port}/api"
        nonce = f"lifecycle-acceptance-{uuid4().hex}"
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(
                [
                    str(PYTHON),
                    "-c",
                    "from sibyl.cli import main; main()",
                    "serve",
                    "--embedded",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=self.home,
                env={**isolated_env(self.home), "SIBYL_GIT_COMMIT": nonce},
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 90
        while self.process.poll() is None:
            try:
                response = httpx.get(self.upstream_url + "/health", timeout=2, trust_env=False)
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code == 200:
                assert response.json()["runtime"]["commit"] == nonce, response.text
                self.record("daemon_started", {"pid": self.process.pid, "nonce": nonce})
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        raise AssertionError(f"Owned daemon failed startup: {self.log_path.read_text()}")

    def stop(self) -> None:
        if self.process is None:
            return
        process = self.process
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        self.record("daemon_stopped", {"pid": process.pid, "returncode": process.returncode})
        self.process = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def close(self) -> None:
        self.stop()
        self.proxy.shutdown()
        self.proxy.server_close()
        self.proxy_thread.join(timeout=5)
        assert not self.proxy_thread.is_alive()
        self.record("cleanup", {"daemon_reaped": True, "proxy_closed": True})

    def pending(self) -> list[dict]:
        return [
            json.loads(path.read_text())
            for path in sorted((self.home / ".config/sibyl/pending_writes").glob("*.json"))
        ]
