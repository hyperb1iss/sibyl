"""Runner daemon - orchestrates connection, agents, and projects."""

import asyncio
from dataclasses import dataclass

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
    started_at: float = 0
    status: str = "initializing"


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
        self._connected_event = asyncio.Event()

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
        log.info("shutdown_requested")
        self._shutdown_requested = True
        self.client.request_shutdown()

    async def run(self) -> None:
        """Run the daemon main loop."""
        log.info(
            "daemon_starting",
            runner_id=self.config.runner_id,
            max_agents=self.config.max_concurrent_agents,
        )

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

        # Start connection with reconnect handling
        await self.client.run_with_reconnect()

        # Cleanup on shutdown
        await self._cleanup()

        log.info("daemon_stopped")

    async def _cleanup(self) -> None:
        """Clean up running executions on shutdown."""
        if not self._executions:
            return

        log.info("cleaning_up_executions", count=len(self._executions))

        # Cancel all running agents
        for execution in list(self._executions.values()):
            await self._cancel_execution(execution)

        self._executions.clear()

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

        # Update status
        await self._update_status()

        # Start execution in background
        task = asyncio.create_task(self._run_agent_execution(execution, task_config))
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
        await self._cancel_execution(execution)
        del self._executions[task_id]

        await self._update_status()

    async def _run_agent_execution(
        self,
        execution: AgentExecution,
        config: dict,
    ) -> None:
        """Run an agent execution to completion.

        This is where the actual agent work happens:
        1. Set up git worktree
        2. Launch Claude Code or other agent
        3. Monitor execution
        4. Report completion
        """
        import time

        execution.started_at = time.time()
        execution.status = "running"

        try:
            # Report starting
            await self.client.send_agent_update(
                execution.agent_id,
                "running",
                progress=0,
                activity="Starting agent execution",
            )

            # TODO: Implement actual agent execution:
            # 1. Setup/find worktree for project
            # 2. Launch Claude Code process with task prompt
            # 3. Monitor stdout/stderr for progress
            # 4. Capture result

            # For now, simulate work
            log.info("agent_execution_started", task_id=execution.task_id)

            # Placeholder: actual agent execution would go here
            await asyncio.sleep(2)  # Simulated work

            # Report completion
            result = {
                "status": "completed",
                "output": "Agent execution completed (stub)",
            }

            await self.client.send_task_complete(execution.task_id, result)
            execution.status = "completed"

            log.info(
                "agent_execution_completed",
                task_id=execution.task_id,
                duration_s=time.time() - execution.started_at,
            )

        except asyncio.CancelledError:
            execution.status = "cancelled"
            log.info("agent_execution_cancelled", task_id=execution.task_id)
            raise

        except Exception as e:
            execution.status = "failed"
            log.exception("agent_execution_failed", task_id=execution.task_id, error=str(e))

            await self.client.send_task_complete(
                execution.task_id,
                {"status": "failed", "error": str(e)},
            )

        finally:
            # Remove from active executions
            if execution.task_id in self._executions:
                del self._executions[execution.task_id]

            await self._update_status()

    async def _cancel_execution(self, execution: AgentExecution) -> None:
        """Cancel a running execution."""
        if execution.process:
            try:
                execution.process.terminate()
                await asyncio.wait_for(execution.process.wait(), timeout=5)
            except TimeoutError:
                execution.process.kill()
            except Exception:
                pass

        execution.status = "cancelled"

    async def _update_status(self) -> None:
        """Send current status to server."""
        if self.current_agent_count >= self.config.max_concurrent_agents:
            status = "busy"
        elif self._shutdown_requested:
            status = "draining"
        else:
            status = "online"

        await self.client.send_status(status, self.current_agent_count)
