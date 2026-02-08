"""Reusable async subprocess executor for agent task execution.

Handles subprocess lifecycle, stdout/stderr streaming with throttle,
timeout (SIGTERM -> SIGKILL), and cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger()


@dataclass
class ExecutionResult:
    """Outcome of a subprocess execution."""

    status: str  # "completed" | "failed" | "timeout" | "cancelled"
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    command: list[str]
    worktree_path: str


# Type alias for the on_output callback:
#   (stream_name: "stdout"|"stderr", line: str) -> Awaitable[None] | None
OutputCallback = Callable[[str, str], Awaitable[None] | None]


@dataclass
class SubprocessExecutor:
    """Async subprocess runner with timeout, cancellation, and output streaming.

    Usage::

        executor = SubprocessExecutor()
        result = await executor.run(
            command=["claude", "--prompt", "hello"],
            cwd=Path("/workspace"),
            env=scrubbed_env,
            timeout_seconds=3600,
            on_output=my_callback,
        )
    """

    # Minimum interval between on_output callbacks (seconds)
    stream_throttle_interval: float = 1.0

    # Max lines retained in stdout/stderr buffers
    max_buffer_lines: int = 200

    # Internal state
    _process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _cancelled: bool = field(default=False, init=False, repr=False)

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        """The underlying subprocess, if running."""
        return self._process

    async def run(
        self,
        command: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        timeout_seconds: int = 3600,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        """Execute a command as an async subprocess.

        Args:
            command: Command + args for ``create_subprocess_exec``.
            cwd: Working directory.
            env: Environment dict (should already be scrubbed).
            timeout_seconds: Max wall-clock time before SIGTERM.
            on_output: Optional callback ``(stream_name, line)`` for live streaming.

        Returns:
            ExecutionResult with status, output, and timing.
        """
        start = time.monotonic()
        self._cancelled = False

        self._process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Collect streams concurrently
        stdout_task = asyncio.create_task(
            self._collect_stream(self._process.stdout, "stdout", on_output)
        )
        stderr_task = asyncio.create_task(
            self._collect_stream(self._process.stderr, "stderr", on_output)
        )

        timed_out = False
        try:
            exit_code = await asyncio.wait_for(self._process.wait(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            log.warning("execution_timeout", timeout=timeout_seconds)
            self._process.terminate()
            try:
                exit_code = await asyncio.wait_for(self._process.wait(), timeout=10)
            except TimeoutError:
                self._process.kill()
                exit_code = await self._process.wait()

        stdout_lines, stderr_lines = await asyncio.gather(stdout_task, stderr_task)
        duration = time.monotonic() - start

        if self._cancelled:
            status = "cancelled"
        elif timed_out:
            status = "timeout"
        elif exit_code == 0:
            status = "completed"
        else:
            status = "failed"

        self._process = None

        return ExecutionResult(
            status=status,
            exit_code=exit_code,
            stdout="\n".join(stdout_lines[-self.max_buffer_lines :]),
            stderr="\n".join(stderr_lines[-self.max_buffer_lines :]),
            duration_s=duration,
            command=command,
            worktree_path=str(cwd),
        )

    async def cancel(self) -> None:
        """Cancel the running subprocess (SIGTERM -> SIGKILL)."""
        self._cancelled = True
        proc = self._process
        if proc is None or proc.returncode is not None:
            return

        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def _collect_stream(
        self,
        stream: asyncio.StreamReader | None,
        stream_name: str,
        on_output: OutputCallback | None,
    ) -> list[str]:
        """Read lines from a stream, invoking on_output with throttle."""
        if stream is None:
            return []

        lines: list[str] = []
        last_callback = 0.0

        while True:
            raw = await stream.readline()
            if not raw:
                break

            text = raw.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue

            lines.append(text)

            if on_output is not None:
                now = time.monotonic()
                if now - last_callback >= self.stream_throttle_interval:
                    last_callback = now
                    result = on_output(stream_name, text)
                    if result is not None:
                        await result

        return lines
