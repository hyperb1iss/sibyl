"""Runner daemon - orchestrates connection, agents, and projects."""

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from sibyl_runner.config import RunnerConfig
from sibyl_runner.connection import RunnerClient
from sibyl_runner.project_registry import ProjectRegistry

log = structlog.get_logger()


@dataclass
class AgentExecution:
    """Tracks a running agent execution."""

    agent_id: str
    task_id: str
    project_id: str
    worktree_path: str
    process: asyncio.subprocess.Process | None = None
    run_task: asyncio.Task[None] | None = None
    started_at: float = 0
    status: str = "initializing"
    completion_sent: bool = False
    cancel_reason: str = ""


class RunnerDaemon:
    """Main runner daemon that manages connection and agent executions.

    Responsibilities:
    - Maintain WebSocket connection to Sibyl Core
    - Receive and execute task assignments
    - Report status and agent updates
    - Manage git worktrees for project isolation
    """

    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.client = RunnerClient(config)
        self.registry = ProjectRegistry(config.worktree_base)
        self._executions: dict[str, AgentExecution] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._shutdown_requested = False
        self._accepting_tasks = True
        self._connected_event = asyncio.Event()
        self._drain_task: asyncio.Task[None] | None = None

        # Register message handlers
        self.client.register_handler("task_assign", self._handle_task_assign)
        self.client.register_handler("task_cancel", self._handle_task_cancel)

        # Register connection callback to signal connected event
        self.client.on_connect(self._connected_event.set)

    @property
    def current_agent_count(self) -> int:
        """Number of currently running agents."""
        return len(self._executions)

    @property
    def available_capacity(self) -> int:
        """Number of additional agents that can be started."""
        return max(0, self.config.max_concurrent_agents - self.current_agent_count)

    def request_shutdown(self) -> None:
        """Request graceful shutdown of daemon."""
        if self._shutdown_requested:
            return

        log.info("shutdown_requested")
        self._shutdown_requested = True
        self._accepting_tasks = False
        self.client.request_shutdown()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if self._drain_task is None or self._drain_task.done():
            self._drain_task = loop.create_task(self._begin_draining("signal"))
            self._background_tasks.add(self._drain_task)
            self._drain_task.add_done_callback(self._background_tasks.discard)

    async def run(self) -> None:
        """Run the daemon main loop."""
        log.info(
            "daemon_starting",
            runner_id=self.config.runner_id,
            max_agents=self.config.max_concurrent_agents,
            sandbox_mode=self.config.sandbox_mode,
            sandbox_id=self.config.sandbox_id or None,
        )

        if not self.config.sandbox_mode:
            # Load projects from config
            project_count = self.registry.load_from_config()
            log.info("projects_loaded", count=project_count)

            # Register project registrations as a background task after connecting
            async def _register_projects() -> None:
                """Register warm projects with server after connection established."""
                # Wait for connection using event instead of polling
                await self._connected_event.wait()

                if self._shutdown_requested:
                    return

                # Register each project's worktree with the server
                for project_id, project in self.registry.projects.items():
                    if project.worktree_path:
                        await self.client.send_project_register(
                            project_id=project_id,
                            worktree_path=str(project.worktree_path),
                            worktree_branch=project.worktree_branch,
                        )

            # Start project registration in background
            task = asyncio.create_task(_register_projects())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        else:
            log.info("sandbox_mode_project_registry_skipped")

        # Start connection with reconnect handling
        await self.client.run_with_reconnect()

        # Cleanup on shutdown
        await self._cleanup()

        log.info("daemon_stopped")

    async def _cleanup(self) -> None:
        """Clean up running executions on shutdown."""
        if self._executions:
            log.info("cleaning_up_executions", count=len(self._executions))
            await self._begin_draining("cleanup")
            self._executions.clear()

        # Ensure background tasks are not left running
        current = asyncio.current_task()
        pending = [t for t in self._background_tasks if t is not current and not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _begin_draining(self, reason: str) -> None:
        """Stop accepting tasks and cancel currently running work."""
        self._accepting_tasks = False
        await self._update_status(forced_status="draining")

        if not self._executions:
            return

        log.info("runner_draining", reason=reason, execution_count=len(self._executions))
        for execution in list(self._executions.values()):
            await self._cancel_execution(execution, reason="runner_draining")

    async def _handle_task_assign(self, data: dict) -> None:
        """Handle task assignment from server.

        Message format:
        {
            "type": "task_assign",
            "task_id": "task_xxx",
            "agent_id": "agent_xxx",
            "project_id": "project_xxx",
            "config": {
                "prompt": "...",
                "repo_url": "...",
                "branch": "...",
                ...
            }
        }
        """
        task_id = data.get("task_id")
        agent_id = data.get("agent_id")
        project_id = data.get("project_id")
        task_config = data.get("config", {})

        # Validate required fields
        if not isinstance(task_id, str) or not isinstance(agent_id, str) or not isinstance(project_id, str):
            log.warning("invalid_task_assign", data=data)
            return

        if not self._accepting_tasks:
            log.info("rejecting_task_while_draining", task_id=task_id)
            await self.client.send({
                "type": "task_reject",
                "task_id": task_id,
                "reason": "runner_draining",
            })
            return

        # Check capacity
        if self.available_capacity <= 0:
            log.warning("at_capacity", task_id=task_id)
            await self.client.send({
                "type": "task_reject",
                "task_id": task_id,
                "reason": "at_capacity",
            })
            return

        log.info(
            "task_assigned",
            task_id=task_id,
            agent_id=agent_id,
            project_id=project_id,
        )

        # Create execution record
        execution = AgentExecution(
            agent_id=agent_id,
            task_id=task_id,
            project_id=project_id,
            worktree_path="",  # Will be set during setup
        )
        self._executions[task_id] = execution

        await self.client.send_task_ack(task_id=task_id, agent_id=agent_id)

        # Update status
        await self._update_status()

        # Start execution in background
        if not isinstance(task_config, dict):
            task_config = {}
        task = asyncio.create_task(self._run_agent_execution(execution, task_config))
        execution.run_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _handle_task_cancel(self, data: dict) -> None:
        """Handle task cancellation from server."""
        task_id = data.get("task_id")

        if not task_id:
            return

        execution = self._executions.get(task_id)
        if not execution:
            log.warning("cancel_unknown_task", task_id=task_id)
            return

        log.info("cancelling_task", task_id=task_id)
        await self._cancel_execution(execution, reason="task_cancel")
        self._executions.pop(task_id, None)

        await self._update_status()

    async def _run_agent_execution(
        self,
        execution: AgentExecution,
        config: dict,
    ) -> None:
        """Run an agent execution to completion.

        MVP harness flow:
        1. Resolve worktree
        2. Run execution command with prompt injected in env
        3. Stream output snippets via agent updates
        4. Send task completion payload
        """
        execution.started_at = time.time()
        execution.status = "running"

        try:
            # Report starting
            await self.client.send_agent_update(
                execution.agent_id,
                "running",
                progress=5,
                activity="Preparing execution harness",
            )

            log.info("agent_execution_started", task_id=execution.task_id)
            prompt = self._extract_prompt(config)
            worktree = await self._resolve_worktree(execution, config)

            result = await self._run_execution_harness(
                execution=execution,
                config=config,
                prompt=prompt,
                worktree=worktree,
            )

            execution.status = result.get("status", "completed")
            await self._send_task_complete_once(execution, result)

            final_status = "completed" if execution.status == "completed" else "failed"
            await self.client.send_agent_update(
                execution.agent_id,
                final_status,
                progress=100 if final_status == "completed" else None,
                activity="Execution finished",
            )

            log.info(
                "agent_execution_completed",
                task_id=execution.task_id,
                duration_s=time.time() - execution.started_at,
            )

        except asyncio.CancelledError:
            execution.status = "cancelled"
            log.info("agent_execution_cancelled", task_id=execution.task_id)
            await self._send_task_complete_once(
                execution,
                {
                    "status": "cancelled",
                    "error": execution.cancel_reason or "execution_cancelled",
                    "duration_s": time.time() - execution.started_at,
                    "worktree_path": execution.worktree_path,
                },
            )
            await self.client.send_agent_update(
                execution.agent_id,
                "cancelled",
                activity="Execution cancelled",
            )
            raise

        except Exception as e:
            execution.status = "failed"
            log.exception("agent_execution_failed", task_id=execution.task_id, error=str(e))

            await self._send_task_complete_once(
                execution,
                {
                    "status": "failed",
                    "error": str(e),
                    "duration_s": time.time() - execution.started_at,
                    "worktree_path": execution.worktree_path,
                },
            )
            await self.client.send_agent_update(
                execution.agent_id,
                "failed",
                activity="Execution failed",
            )

        finally:
            # Remove from active executions
            self._executions.pop(execution.task_id, None)
            execution.process = None

            await self._update_status()

    async def _cancel_execution(self, execution: AgentExecution, reason: str) -> None:
        """Cancel a running execution."""
        execution.cancel_reason = reason

        if execution.process and execution.process.returncode is None:
            try:
                execution.process.terminate()
                await asyncio.wait_for(execution.process.wait(), timeout=5)
            except TimeoutError:
                execution.process.kill()
                with contextlib.suppress(Exception):
                    await execution.process.wait()
            except Exception:
                pass

        run_task = execution.run_task
        current = asyncio.current_task()
        if run_task and not run_task.done() and run_task is not current:
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

        execution.status = "cancelled"
        await self._send_task_complete_once(
            execution,
            {
                "status": "cancelled",
                "error": reason,
                "duration_s": max(0.0, time.time() - execution.started_at),
                "worktree_path": execution.worktree_path,
            },
        )
        await self.client.send_agent_update(
            execution.agent_id,
            "cancelled",
            activity="Execution cancelled",
        )

    async def _update_status(self, forced_status: str | None = None) -> None:
        """Send current status to server."""
        if forced_status:
            status = forced_status
        elif self.current_agent_count >= self.config.max_concurrent_agents:
            status = "busy"
        elif self._shutdown_requested:
            status = "draining"
        else:
            status = "online"

        await self.client.send_status(status, self.current_agent_count)

    async def _send_task_complete_once(self, execution: AgentExecution, result: dict[str, Any]) -> None:
        """Send task completion at most once per execution."""
        if execution.completion_sent:
            return
        execution.completion_sent = True
        await self.client.send_task_complete(execution.task_id, result)

    def _extract_prompt(self, config: dict[str, Any]) -> str:
        """Extract prompt from task config."""
        prompt = config.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt.strip()

        title = config.get("title")
        description = config.get("description")
        if isinstance(title, str) and isinstance(description, str):
            return f"{title}\n\n{description}".strip()
        if isinstance(title, str) and title.strip():
            return title.strip()

        return ""

    async def _resolve_worktree(self, execution: AgentExecution, config: dict[str, Any]) -> Path:
        """Resolve worktree path for execution and ensure it exists."""
        config_worktree = config.get("worktree_path")
        if isinstance(config_worktree, str) and config_worktree.strip():
            worktree = Path(config_worktree)
            worktree.mkdir(parents=True, exist_ok=True)
            execution.worktree_path = str(worktree)
            return worktree

        branch = config.get("branch")
        resolved_branch = branch if isinstance(branch, str) else None

        # For registered mode, prefer linked project worktrees when available.
        worktree: Path | None = None
        if not self.config.sandbox_mode:
            worktree = await self.registry.ensure_worktree(
                execution.project_id,
                branch=resolved_branch,
                task_id=execution.task_id,
            )

        if worktree is None:
            # Sandbox mode and unknown projects both use isolated local directories.
            worktree = self.config.worktree_base / execution.project_id / execution.task_id
            worktree.mkdir(parents=True, exist_ok=True)

        execution.worktree_path = str(worktree)
        return worktree

    def _resolve_execution_command(self, config: dict[str, Any]) -> list[str] | None:
        """Resolve command used for the execution harness."""
        command = config.get("command") or config.get("exec_command") or config.get("runner_command")

        if isinstance(command, list) and command and all(isinstance(item, str) for item in command):
            return command
        if isinstance(command, str) and command.strip():
            return ["/bin/sh", "-lc", command]

        env_cmd = os.environ.get("SIBYL_RUNNER_EXEC_CMD")
        if env_cmd:
            return ["/bin/sh", "-lc", env_cmd]

        # MVP fallback still executes through a concrete subprocess path.
        return ["/bin/sh", "-lc", 'printf "%s\\n" "$SIBYL_TASK_PROMPT"']

    async def _run_execution_harness(
        self,
        execution: AgentExecution,
        config: dict[str, Any],
        prompt: str,
        worktree: Path,
    ) -> dict[str, Any]:
        """Execute task through subprocess harness, fallback to internal callable."""
        command = self._resolve_execution_command(config)
        if not command:
            return await self._run_internal_callable(execution, prompt, worktree)

        env = os.environ.copy()
        env["SIBYL_TASK_ID"] = execution.task_id
        env["SIBYL_AGENT_ID"] = execution.agent_id
        env["SIBYL_PROJECT_ID"] = execution.project_id
        env["SIBYL_TASK_PROMPT"] = prompt
        env["SIBYL_WORKTREE_PATH"] = str(worktree)
        if self.config.sandbox_id:
            env["SIBYL_SANDBOX_ID"] = self.config.sandbox_id

        await self.client.send_agent_update(
            execution.agent_id,
            "running",
            progress=20,
            activity="Launching execution subprocess",
        )

        try:
            execution.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(worktree),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            log.warning("execution_command_not_found", command=command[0])
            return await self._run_internal_callable(execution, prompt, worktree)

        stdout_task = asyncio.create_task(
            self._collect_stream(execution=execution, stream=execution.process.stdout, stream_name="stdout")
        )
        stderr_task = asyncio.create_task(
            self._collect_stream(execution=execution, stream=execution.process.stderr, stream_name="stderr")
        )
        self._background_tasks.add(stdout_task)
        self._background_tasks.add(stderr_task)
        stdout_task.add_done_callback(self._background_tasks.discard)
        stderr_task.add_done_callback(self._background_tasks.discard)

        exit_code = await execution.process.wait()
        stdout_lines, stderr_lines = await asyncio.gather(stdout_task, stderr_task)

        status = "completed" if exit_code == 0 else "failed"
        return {
            "status": status,
            "exit_code": exit_code,
            "command": command,
            "worktree_path": str(worktree),
            "stdout": "\n".join(stdout_lines[-200:]),
            "stderr": "\n".join(stderr_lines[-200:]),
            "duration_s": time.time() - execution.started_at,
        }

    async def _collect_stream(
        self,
        execution: AgentExecution,
        stream: asyncio.StreamReader | None,
        stream_name: str,
    ) -> list[str]:
        """Collect subprocess stream output and send throttled status updates."""
        if stream is None:
            return []

        lines: list[str] = []
        last_update = 0.0

        while True:
            raw = await stream.readline()
            if not raw:
                break

            text = raw.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue

            lines.append(text)
            now = time.monotonic()
            if now - last_update >= 1.0:
                last_update = now
                await self.client.send_agent_update(
                    execution.agent_id,
                    "running",
                    activity=f"{stream_name}: {text[:200]}",
                )

        return lines

    async def _run_internal_callable(
        self,
        execution: AgentExecution,
        prompt: str,
        worktree: Path,
    ) -> dict[str, Any]:
        """Fallback execution path when subprocess command is unavailable."""
        await self.client.send_agent_update(
            execution.agent_id,
            "running",
            progress=50,
            activity="Running internal fallback executor",
        )

        prompt_file = worktree / f"{execution.task_id}.prompt.txt"
        prompt_file.write_text(prompt or "(empty prompt)", encoding="utf-8")

        await asyncio.sleep(0)

        return {
            "status": "completed",
            "worktree_path": str(worktree),
            "output": f"Prompt captured at {prompt_file}",
            "duration_s": time.time() - execution.started_at,
        }
