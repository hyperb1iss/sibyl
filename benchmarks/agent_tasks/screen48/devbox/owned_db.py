"""Drive the owned restored SurrealDB container over the devbox Docker socket.

Standard library only: ``http.client`` speaking the Docker Engine API across an
``AF_UNIX`` socket, and a plain HTTP poll of SurrealDB's ``/health`` endpoint.
No docker CLI, no third-party client.

The container is the restored full-cohort database (image
``surrealdb/surrealdb:v3.2.3``, volume
``sibyl14-full-cohort-restore-5c53857b613f4c5caa8fd156cb56df26-restored-data``).
Every lifecycle call first inspects the container and refuses to act on one
whose name does not carry the restore prefix, so a mistyped id cannot start or
stop somebody else's database.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
from typing import Any
from urllib.parse import urlsplit

DEFAULT_SOCKET_PATH = "/run/devbox-docker/docker.sock"
DEFAULT_CONTAINER_ID = "4e76d720d420"
DEFAULT_HOST_PORT = 21642
OWNED_NAME_PREFIX = "/sibyl14-full-cohort-restore-5c53857b"

_DOCKER_TIMEOUT_SECONDS = 30.0
_HEALTH_TIMEOUT_SECONDS = 5.0
_HEALTH_POLL_SECONDS = 0.5

_HTTP_OK = 200
_HTTP_NO_CONTENT = 204
_HTTP_NOT_MODIFIED = 304


class OwnedDatabaseError(RuntimeError):
    """The owned database could not be inspected or driven."""


class ForeignContainerError(OwnedDatabaseError):
    """The inspected container is not the owned restore; refusing to touch it."""


class _UnixSocketConnection(http.client.HTTPConnection):
    """HTTP over an ``AF_UNIX`` stream socket, which is how dockerd listens."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            sock.close()
            raise OwnedDatabaseError(
                f"cannot reach docker socket {self.socket_path}: {exc}"
            ) from exc
        self.sock = sock


def _docker_request(
    method: str,
    path: str,
    *,
    socket_path: str = DEFAULT_SOCKET_PATH,
    timeout: float = _DOCKER_TIMEOUT_SECONDS,
) -> tuple[int, bytes]:
    connection = _UnixSocketConnection(socket_path, timeout)
    try:
        connection.request(method, path, headers={"Host": "docker", "Accept": "application/json"})
        response = connection.getresponse()
        return response.status, response.read()
    except OSError as exc:
        raise OwnedDatabaseError(f"docker {method} {path} failed: {exc}") from exc
    finally:
        connection.close()


def inspect(
    container_id: str = DEFAULT_CONTAINER_ID,
    *,
    socket_path: str = DEFAULT_SOCKET_PATH,
) -> dict[str, Any]:
    """Return the container's inspect document."""
    status, body = _docker_request(
        "GET", f"/containers/{container_id}/json", socket_path=socket_path
    )
    if status != _HTTP_OK:
        raise OwnedDatabaseError(f"inspect {container_id} returned HTTP {status}: {body[:200]!r}")
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise OwnedDatabaseError(f"inspect {container_id} returned non-JSON") from exc
    if not isinstance(document, dict):
        raise OwnedDatabaseError(f"inspect {container_id} returned {type(document).__name__}")
    return document


def assert_owned(document: dict[str, Any]) -> str:
    """Refuse any container whose name is not the owned restore."""
    name = document.get("Name")
    if not isinstance(name, str) or not name.startswith(OWNED_NAME_PREFIX):
        raise ForeignContainerError(
            f"container name {name!r} does not start with {OWNED_NAME_PREFIX!r}"
        )
    return name


def start(
    container_id: str = DEFAULT_CONTAINER_ID,
    *,
    socket_path: str = DEFAULT_SOCKET_PATH,
) -> dict[str, Any]:
    """Start the owned container and return the inspect document taken first."""
    document = inspect(container_id, socket_path=socket_path)
    assert_owned(document)
    status, body = _docker_request(
        "POST", f"/containers/{container_id}/start", socket_path=socket_path
    )
    if status not in {_HTTP_NO_CONTENT, _HTTP_NOT_MODIFIED}:
        raise OwnedDatabaseError(f"start {container_id} returned HTTP {status}: {body[:200]!r}")
    return document


def stop(
    container_id: str = DEFAULT_CONTAINER_ID,
    timeout: int = 30,
    *,
    socket_path: str = DEFAULT_SOCKET_PATH,
) -> dict[str, Any]:
    """Stop the owned container and return the inspect document taken after."""
    document = inspect(container_id, socket_path=socket_path)
    assert_owned(document)
    status, body = _docker_request(
        "POST",
        f"/containers/{container_id}/stop?t={int(timeout)}",
        socket_path=socket_path,
        timeout=max(_DOCKER_TIMEOUT_SECONDS, float(timeout) + 10.0),
    )
    if status not in {_HTTP_NO_CONTENT, _HTTP_NOT_MODIFIED}:
        raise OwnedDatabaseError(f"stop {container_id} returned HTTP {status}: {body[:200]!r}")
    return inspect(container_id, socket_path=socket_path)


def health_port(url_ws: str, *, default_port: int = DEFAULT_HOST_PORT) -> tuple[str, int]:
    """Read the host and port a ``ws://host:port/rpc`` URL points at."""
    parts = urlsplit(url_ws)
    return parts.hostname or "127.0.0.1", parts.port or default_port


def probe_health(host: str, port: int, *, timeout: float = _HEALTH_TIMEOUT_SECONDS) -> int | None:
    """Return the ``/health`` status code, or ``None`` when unreachable."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", "/health")
        return connection.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def wait_ready(url_ws: str, timeout: float = 120.0) -> dict[str, Any]:
    """Poll SurrealDB's HTTP health endpoint until it answers 200."""
    host, port = health_port(url_ws)
    deadline = time.monotonic() + timeout
    attempts = 0
    last: int | None = None
    while time.monotonic() < deadline:
        attempts += 1
        last = probe_health(host, port)
        if last == _HTTP_OK:
            return {"host": host, "port": port, "attempts": attempts, "status": last}
        time.sleep(_HEALTH_POLL_SECONDS)
    raise OwnedDatabaseError(
        f"surreal health at {host}:{port} never returned 200 within {timeout}s (last {last})"
    )


__all__ = [
    "DEFAULT_CONTAINER_ID",
    "DEFAULT_HOST_PORT",
    "DEFAULT_SOCKET_PATH",
    "OWNED_NAME_PREFIX",
    "ForeignContainerError",
    "OwnedDatabaseError",
    "assert_owned",
    "health_port",
    "inspect",
    "probe_health",
    "start",
    "stop",
    "wait_ready",
]
