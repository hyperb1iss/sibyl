"""Agent Harness infrastructure for AI agent orchestration.

This module provides the runtime infrastructure for managing AI agents:
- WorktreeManager: Isolated git worktrees for parallel agent development
- IntegrationManager: Merge orchestration for worktree branches
- AgentRunner: Claude Agent SDK integration for spawning and managing agents
- ApprovalService: Human-in-the-loop approval hooks for dangerous operations
- CheckpointManager: Session state persistence for agent recovery
- OrchestratorService: Multi-agent coordination (coming soon)
- messages: Message formatting for UI display
"""

from sibyl.agents.approvals import ApprovalService
from sibyl.agents.checkpoints import (
    CheckpointManager,
    CheckpointRestoreError,
    RestoreResult,
    create_checkpoint_from_instance,
    restore_from_checkpoint,
)
from sibyl.agents.integration import (
    ConflictError,
    IntegrationError,
    IntegrationManager,
    IntegrationResult,
    IntegrationStatus,
    TestConfig,
    TestFailedError,
)
from sibyl.agents.message_bus import (
    MessageBus,
    format_message_for_injection,
    format_messages_digest,
)
from sibyl.agents.messages import (
    format_agent_message,
    format_assistant_message,
    format_user_message,
    generate_workflow_reminder,
    get_tool_icon_and_preview,
)
from sibyl.agents.orchestrator import AgentOrchestrator, OrchestratorError
from sibyl.agents.runner import AgentInstance, AgentRunner, AgentRunnerError
from sibyl.agents.worktree import SetupConfig, SetupResult, WorktreeError, WorktreeManager

__all__ = [
    "AgentInstance",
    "AgentOrchestrator",
    "AgentRunner",
    "AgentRunnerError",
    "ApprovalService",
    "CheckpointManager",
    "CheckpointRestoreError",
    "ConflictError",
    "IntegrationError",
    "IntegrationManager",
    "IntegrationResult",
    "IntegrationStatus",
    "MessageBus",
    "OrchestratorError",
    "RestoreResult",
    "SetupConfig",
    "SetupResult",
    "TestConfig",
    "TestFailedError",
    "WorktreeError",
    "WorktreeManager",
    "create_checkpoint_from_instance",
    "format_agent_message",
    "format_assistant_message",
    "format_message_for_injection",
    "format_messages_digest",
    "format_user_message",
    "generate_workflow_reminder",
    "get_tool_icon_and_preview",
    "restore_from_checkpoint",
]
