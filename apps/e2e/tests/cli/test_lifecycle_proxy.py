"""Real HTTP checks for proxy transparency and the explicit ACK-loss boundary."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.cli.lifecycle_runtime import LifecycleRuntime

PATH = "/api/memory/raw"
KEY = "9cf0a19e-094f-43c0-aaf4-bf538b8ddc5a"
RECEIPT = {
    "operation_id": KEY,
    "applied": True,
    "revision": 1,
    "affected_records": ["raw_captures:protocol-fixture"],
    "idempotency_key": KEY,
    "replayed": False,
}


@pytest.fixture(autouse=True)
def require_services() -> None:
    """Proxy protocol checks do not start or contact a product daemon."""


@pytest.fixture
def proxy_runtime(tmp_path) -> Iterator[LifecycleRuntime]:
    runtime = LifecycleRuntime(tmp_path)
    runtime.drop_path = PATH
    try:
        yield runtime
    finally:
        runtime.close()


@contextmanager
def upstream_response(
    status: int, body: bytes | None, content_type: str | None = None
) -> Iterator[str]:
    """Serve one controlled protocol response over an owned HTTP listener."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", "0")))
            if body is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            self.send_response(status)
            if content_type is not None:
                self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.cli
@pytest.mark.parametrize(
    ("status", "body", "content_type"),
    [
        pytest.param(500, b"upstream failed\n", "text/plain; charset=utf-8", id="plain-text-error"),
        pytest.param(204, b"", None, id="empty-response"),
        pytest.param(200, b'{"ok": true}', "application/json", id="ordinary-json"),
        pytest.param(
            500,
            json.dumps({"mutation_receipt": RECEIPT}).encode(),
            "application/json",
            id="error-with-receipt",
        ),
        pytest.param(
            200,
            json.dumps({"mutation_receipt": {**RECEIPT, "applied": False}}).encode(),
            "application/json",
            id="unapplied-receipt",
        ),
        pytest.param(
            200,
            json.dumps({"mutation_receipt": {**RECEIPT, "idempotency_key": "other"}}).encode(),
            "application/json",
            id="different-key",
        ),
        pytest.param(
            200, b'{"mutation_receipt": "malformed"}', "application/json", id="malformed-receipt"
        ),
    ],
)
def test_forwarded_response_preserves_fault_arm(
    proxy_runtime: LifecycleRuntime, status: int, body: bytes, content_type: str | None
) -> None:
    runtime = proxy_runtime
    with upstream_response(status, body, content_type) as upstream:
        runtime.upstream_url = upstream
        response = httpx.post(
            runtime.proxy_url.removesuffix("/api") + PATH,
            headers={"Idempotency-Key": KEY},
            json={},
            timeout=5,
            trust_env=False,
        )
    assert response.status_code == status
    assert response.content == body
    assert response.headers.get("content-type") == content_type
    assert runtime.drop_path == PATH
    assert len(runtime.requests) == 1
    assert not runtime.requests[0].get("ack_dropped_after_applied_receipt")
    assert not any(event["step"] == "upstream_http_error" for event in runtime.events)


@pytest.mark.cli
def test_upstream_disconnect_returns_502_without_injected_loss(
    proxy_runtime: LifecycleRuntime,
) -> None:
    runtime = proxy_runtime
    with upstream_response(200, None) as upstream:
        runtime.upstream_url = upstream
        response = httpx.post(
            runtime.proxy_url.removesuffix("/api") + PATH,
            headers={"Idempotency-Key": KEY},
            json={},
            timeout=5,
            trust_env=False,
        )
    assert response.status_code == 502
    assert response.content == b"Upstream request failed.\n"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    errors = [event for event in runtime.events if event["step"] == "upstream_http_error"]
    assert len(errors) == 1
    assert errors[0]["error_type"] == "RemoteProtocolError"
    assert runtime.drop_path == PATH
    assert not runtime.requests[0].get("ack_dropped_after_applied_receipt")


@pytest.mark.cli
def test_verified_applied_receipt_drops_only_its_ack(proxy_runtime: LifecycleRuntime) -> None:
    runtime = proxy_runtime
    body = json.dumps({"mutation_receipt": RECEIPT}).encode()
    with upstream_response(200, body, "application/json") as upstream:
        runtime.upstream_url = upstream
        with pytest.raises(httpx.RemoteProtocolError):
            httpx.post(
                runtime.proxy_url.removesuffix("/api") + PATH,
                headers={"Idempotency-Key": KEY},
                json={},
                timeout=5,
                trust_env=False,
            )
    assert runtime.drop_path is None
    assert len(runtime.requests) == 1
    observed = runtime.requests[0]
    assert observed["status"] == 200
    assert observed["response"]["mutation_receipt"] == RECEIPT
    assert observed["ack_dropped_after_applied_receipt"] is True
    assert not any(event["step"] == "upstream_http_error" for event in runtime.events)
