"""Bidirectional relay between browser WebSocket and K8s pod exec."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from fastapi import WebSocket, WebSocketDisconnect

log = structlog.get_logger()


class SandboxExecProxy:
    """Bidirectional relay between browser WebSocket and K8s pod exec."""

    def __init__(self, namespace: str = "default") -> None:
        self._namespace = namespace
        self._ws_client: Any | None = None
        self._k8s_stream: Any | None = None

    async def connect(self, pod_name: str, namespace: str | None = None, command: list[str] | None = None) -> None:
        """Open K8s exec stream to the given pod."""
        ns = namespace or self._namespace
        cmd = command or ["/bin/sh"]

        try:
            from kubernetes_asyncio import client as k8s_client
            from kubernetes_asyncio.stream import WsApiClient
        except ImportError as e:
            raise RuntimeError("kubernetes_asyncio is required for exec proxy") from e

        ws_api = WsApiClient()
        self._ws_client = ws_api
        core_api = k8s_client.CoreV1Api(api_client=ws_api)

        self._k8s_stream = await core_api.connect_get_namespaced_pod_exec(
            name=pod_name,
            namespace=ns,
            command=cmd,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
            _preload_content=False,
        )
        log.info("sandbox_exec_connected", pod_name=pod_name, namespace=ns, command=cmd)

    async def relay(self, browser_ws: WebSocket) -> None:
        """Run bidirectional relay until either side disconnects."""
        if self._k8s_stream is None:
            raise RuntimeError("K8s exec stream not connected; call connect() first")

        stream = self._k8s_stream

        async def _browser_to_pod() -> None:
            """Forward browser stdin to K8s pod."""
            try:
                while True:
                    data = await browser_ws.receive_text()
                    # Check for resize control message
                    try:
                        msg = json.loads(data)
                        if isinstance(msg, dict) and msg.get("type") == "resize":
                            cols = int(msg.get("cols", 80))
                            rows = int(msg.get("rows", 24))
                            await self.resize(cols, rows)
                            continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                    # Forward raw stdin to pod
                    if stream.is_open():
                        # Channel 0 = stdin
                        await stream.write_stdin(data)
            except WebSocketDisconnect:
                log.debug("sandbox_exec_browser_disconnected")
            except Exception as e:
                log.warning("sandbox_exec_browser_to_pod_error", error=str(e))

        async def _pod_to_browser() -> None:
            """Forward K8s pod stdout/stderr to browser."""
            try:
                while stream.is_open():
                    # read_stdout reads from stdout channel
                    output = await stream.read_stdout(timeout=1)
                    if output:
                        await browser_ws.send_text(output)
                    # Also read stderr
                    err_output = await stream.read_stderr(timeout=0.1)
                    if err_output:
                        await browser_ws.send_text(err_output)
            except WebSocketDisconnect:
                log.debug("sandbox_exec_pod_browser_disconnected")
            except Exception as e:
                log.warning("sandbox_exec_pod_to_browser_error", error=str(e))

        # Run both directions concurrently
        tasks = [
            asyncio.create_task(_browser_to_pod()),
            asyncio.create_task(_pod_to_browser()),
        ]
        try:
            # Wait for either direction to complete (first one to end stops both)
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            # Re-raise any exception from completed tasks
            for task in done:
                if task.exception():
                    log.warning("sandbox_exec_relay_error", error=str(task.exception()))
        finally:
            await self.close()

    async def resize(self, cols: int, rows: int) -> None:
        """Send resize control message to K8s exec stream (channel 4)."""
        if self._k8s_stream is None:
            return
        try:
            resize_msg = json.dumps({"Width": cols, "Height": rows})
            # Channel 4 is the resize channel in K8s exec protocol
            await self._k8s_stream.write_channel(4, resize_msg)
            log.debug("sandbox_exec_resized", cols=cols, rows=rows)
        except Exception as e:
            log.warning("sandbox_exec_resize_failed", error=str(e))

    async def close(self) -> None:
        """Clean up K8s connection."""
        if self._k8s_stream is not None:
            try:
                self._k8s_stream.close()
            except Exception:
                pass
            self._k8s_stream = None
        if self._ws_client is not None:
            try:
                await self._ws_client.close()
            except Exception:
                pass
            self._ws_client = None
        log.debug("sandbox_exec_closed")
