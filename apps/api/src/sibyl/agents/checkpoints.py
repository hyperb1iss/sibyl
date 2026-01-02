"""AgentCheckpointManager for session state persistence.

Provides checkpointing and restore capabilities for agent sessions,
enabling agents to survive system restarts and resume from saved state.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from sibyl_core.models import AgentCheckpoint, EntityType

if TYPE_CHECKING:
    from sibyl.agents.runner import AgentInstance
    from sibyl_core.graph import EntityManager

log = structlog.get_logger()


def _generate_checkpoint_id(agent_id: str, timestamp: str) -> str:
    """Generate a unique checkpoint ID."""
    combined = f"{agent_id}:{timestamp}"
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"checkpoint_{hash_bytes}"


class CheckpointManager:
    """Manages agent session checkpoints for persistence and recovery.

    Checkpoints capture everything needed to resume an agent:
    - Conversation history (serialized messages)
    - Pending tool calls
    - Files modified and uncommitted changes
    - Current execution step
    - Pending approvals or task dependencies

    Checkpoints are stored in the knowledge graph and can be used
    to restore agent state after system restart.
    """

    def __init__(
        self,
        entity_manager: "EntityManager",
        agent_id: str,
    ):
        """Initialize CheckpointManager.

        Args:
            entity_manager: Graph client for persistence
            agent_id: Agent UUID to manage checkpoints for
        """
        self.entity_manager = entity_manager
        self.agent_id = agent_id

    async def checkpoint(
        self,
        instance: "AgentInstance",
        current_step: str | None = None,
        pending_approval_id: str | None = None,
    ) -> AgentCheckpoint:
        """Create a checkpoint from the current agent state.

        Args:
            instance: Running agent instance to checkpoint
            current_step: Optional step description
            pending_approval_id: Optional approval blocking the agent

        Returns:
            Created AgentCheckpoint record
        """
        timestamp = datetime.now(UTC).isoformat()
        checkpoint_id = _generate_checkpoint_id(self.agent_id, timestamp)

        # Get conversation history from instance
        conversation_history = instance.get_conversation_history()

        # Get uncommitted changes from worktree if available
        uncommitted_changes = ""
        files_modified: list[str] = []

        if instance.worktree_path and instance.worktree_path.exists():
            uncommitted_changes, files_modified = await self._get_git_state(instance.worktree_path)

        # Build checkpoint record
        checkpoint = AgentCheckpoint(
            id=checkpoint_id,
            name=f"checkpoint-{self.agent_id[-8:]}",
            agent_id=self.agent_id,
            session_id=instance.session_id or "",
            conversation_history=conversation_history,
            pending_tool_calls=[],  # SDK handles this internally
            files_modified=files_modified,
            uncommitted_changes=uncommitted_changes,
            current_step=current_step,
            completed_steps=[],  # Could track from instance if needed
            pending_approval_id=pending_approval_id,
        )

        # Persist to graph (fast direct insert, no LLM extraction needed)
        await self.entity_manager.create_direct(checkpoint, generate_embedding=False)

        # Update agent record with latest checkpoint reference
        await self.entity_manager.update(
            self.agent_id,
            {"last_checkpoint": checkpoint_id},
        )

        log.info(f"Created checkpoint {checkpoint_id} for agent {self.agent_id}")
        return checkpoint

    async def _get_git_state(self, worktree_path: Path) -> tuple[str, list[str]]:
        """Get uncommitted changes and modified files from worktree.

        Args:
            worktree_path: Path to git worktree

        Returns:
            Tuple of (diff_output, list_of_modified_files)
        """
        import asyncio

        # Get list of modified files
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        # Parse modified files from git status output (format: "XY filename")
        files_modified = [line[3:].strip() for line in stdout.decode().strip().split("\n") if line]

        # Get diff of uncommitted changes
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "HEAD",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        diff_stdout, _ = await proc.communicate()
        uncommitted_changes = diff_stdout.decode()

        # Limit diff size to avoid storing huge diffs
        max_diff_size = 100_000  # 100KB
        if len(uncommitted_changes) > max_diff_size:
            uncommitted_changes = (
                uncommitted_changes[:max_diff_size]
                + f"\n... [truncated, {len(uncommitted_changes)} bytes total]"
            )

        return uncommitted_changes, files_modified

    async def get_latest(self) -> AgentCheckpoint | None:
        """Get the most recent checkpoint for this agent.

        Returns:
            Latest AgentCheckpoint or None if no checkpoints exist
        """
        checkpoints = await self.list_checkpoints(limit=1)
        return checkpoints[0] if checkpoints else None

    async def list_checkpoints(self, limit: int = 10) -> list[AgentCheckpoint]:
        """List checkpoints for this agent, most recent first.

        Args:
            limit: Maximum number of checkpoints to return

        Returns:
            List of AgentCheckpoint records, sorted by creation time descending
        """
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.CHECKPOINT,
            limit=limit * 5,  # Fetch extra to filter
        )

        # Filter to this agent's checkpoints
        agent_checkpoints = [
            r for r in results if isinstance(r, AgentCheckpoint) and r.agent_id == self.agent_id
        ]

        # Sort by created_at descending (most recent first)
        agent_checkpoints.sort(key=lambda c: c.created_at or "", reverse=True)

        return agent_checkpoints[:limit]

    async def get_checkpoint(self, checkpoint_id: str) -> AgentCheckpoint | None:
        """Get a specific checkpoint by ID.

        Args:
            checkpoint_id: Checkpoint UUID

        Returns:
            AgentCheckpoint or None if not found
        """
        try:
            entity = await self.entity_manager.get(checkpoint_id)
            if entity and isinstance(entity, AgentCheckpoint):
                return entity
        except Exception:
            log.debug(f"Checkpoint not found: {checkpoint_id}")
        return None

    async def cleanup_old(self, keep_count: int = 5) -> int:
        """Delete old checkpoints, keeping the most recent ones.

        Args:
            keep_count: Number of recent checkpoints to keep

        Returns:
            Number of checkpoints deleted
        """
        checkpoints = await self.list_checkpoints(limit=100)

        if len(checkpoints) <= keep_count:
            return 0

        # Delete older checkpoints
        to_delete = checkpoints[keep_count:]
        deleted = 0

        for checkpoint in to_delete:
            try:
                await self.entity_manager.delete(checkpoint.id)
                deleted += 1
            except Exception:
                log.exception(f"Failed to delete checkpoint {checkpoint.id}")

        if deleted:
            log.info(f"Cleaned up {deleted} old checkpoints for agent {self.agent_id}")

        return deleted


class CheckpointRestoreError(Exception):
    """Error during checkpoint restoration."""


@dataclass
class RestoreResult:
    """Result of restoring an agent from checkpoint."""

    checkpoint: AgentCheckpoint
    worktree_path: Path | None
    session_id: str
    pending_approval_id: str | None
    has_uncommitted_changes: bool


async def restore_from_checkpoint(
    entity_manager: "EntityManager",
    checkpoint: AgentCheckpoint,
) -> RestoreResult:
    """Prepare restoration data from a checkpoint.

    This validates the checkpoint and returns the data needed
    to resume an agent. The actual agent creation should be done
    by AgentRunner.resume_from_checkpoint().

    Args:
        entity_manager: Graph client
        checkpoint: Checkpoint to restore from

    Returns:
        RestoreResult with validated restoration data

    Raises:
        CheckpointRestoreError: If checkpoint cannot be restored
    """
    # Get the agent record
    agent = await entity_manager.get(checkpoint.agent_id)
    if not agent:
        raise CheckpointRestoreError(f"Agent not found: {checkpoint.agent_id}")

    # Check worktree path
    worktree_path: Path | None = None
    worktree_path_str = getattr(agent, "worktree_path", None)
    if worktree_path_str:
        worktree_path = Path(worktree_path_str)
        if not worktree_path.exists():
            log.warning(f"Worktree no longer exists: {worktree_path}")
            worktree_path = None

    # Validate session ID
    if not checkpoint.session_id:
        raise CheckpointRestoreError("Checkpoint has no session ID - cannot resume")

    log.info(f"Prepared restore for agent {checkpoint.agent_id} from checkpoint {checkpoint.id}")

    return RestoreResult(
        checkpoint=checkpoint,
        worktree_path=worktree_path,
        session_id=checkpoint.session_id,
        pending_approval_id=checkpoint.pending_approval_id,
        has_uncommitted_changes=bool(checkpoint.uncommitted_changes),
    )


async def create_checkpoint_from_instance(
    entity_manager: "EntityManager",
    instance: "AgentInstance",
    current_step: str | None = None,
    pending_approval_id: str | None = None,
) -> AgentCheckpoint:
    """Convenience function to checkpoint an agent instance.

    Args:
        entity_manager: Graph client
        instance: Running agent instance
        current_step: Optional step description
        pending_approval_id: Optional approval blocking the agent

    Returns:
        Created AgentCheckpoint
    """
    manager = CheckpointManager(entity_manager, instance.id)
    return await manager.checkpoint(
        instance,
        current_step=current_step,
        pending_approval_id=pending_approval_id,
    )
