"""ApprovalService for human-in-the-loop approvals.

Implements Claude Agent SDK hooks that intercept dangerous operations
and create approval requests for human review.
"""

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    SyncHookJSONOutput,
)

from sibyl_core.models import (
    ApprovalRecord,
    ApprovalStatus,
    ApprovalType,
    EntityType,
)

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

logger = logging.getLogger(__name__)

# Patterns for dangerous operations
DESTRUCTIVE_BASH_PATTERNS = [
    r"\brm\s+(-[rf]+\s+)*[^\s]+",  # rm commands
    r"\bgit\s+push\s+.*--force",  # force push
    r"\bgit\s+push\s+-f\b",  # force push short
    r"\bgit\s+reset\s+--hard",  # hard reset
    r"\bgit\s+clean\s+-fd",  # clean untracked
    r"\bdrop\s+database\b",  # SQL drop
    r"\bdrop\s+table\b",  # SQL drop table
    r"\btruncate\s+table\b",  # SQL truncate
    r"\bkubectl\s+delete\b",  # k8s delete
    r"\bdocker\s+rm\b",  # docker remove
    r"\bdocker\s+system\s+prune",  # docker prune
]

SENSITIVE_FILE_PATTERNS = [
    r"\.env",  # Environment files
    r"\.env\.[a-z]+",  # .env.local, .env.production
    r"secrets?\.[a-z]+$",  # secrets.yaml, secret.json
    r"credentials?\.[a-z]+$",  # credentials.json
    r"\.pem$",  # Private keys
    r"\.key$",  # Private keys
    r"id_rsa",  # SSH keys
    r"id_ed25519",  # SSH keys
    r"password",  # Password files
    r"token",  # Token files
]

# External domains that need approval
EXTERNAL_API_APPROVAL_DOMAINS = [
    r"api\.",  # Any API endpoint
    r"webhook",  # Webhook endpoints
    r"\.slack\.com",  # Slack
    r"\.stripe\.com",  # Payment
    r"\.twilio\.com",  # SMS
    r"\.sendgrid\.com",  # Email
]

# Default approval timeout
DEFAULT_APPROVAL_TIMEOUT = timedelta(hours=24)


def _generate_approval_id(agent_id: str, tool_name: str, timestamp: str) -> str:
    """Generate a unique approval ID."""
    combined = f"{agent_id}:{tool_name}:{timestamp}"
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"approval_{hash_bytes}"


class ApprovalService:
    """Manages human-in-the-loop approvals for agent operations.

    Creates hooks that intercept dangerous operations and block
    execution until human approval is received.
    """

    def __init__(
        self,
        entity_manager: "EntityManager",
        org_id: str,
        project_id: str,
        agent_id: str,
        task_id: str | None = None,
    ):
        """Initialize ApprovalService.

        Args:
            entity_manager: Graph client for persistence
            org_id: Organization UUID
            project_id: Project UUID
            agent_id: Agent UUID requesting approvals
            task_id: Optional task UUID for context
        """
        self.entity_manager = entity_manager
        self.org_id = org_id
        self.project_id = project_id
        self.agent_id = agent_id
        self.task_id = task_id

        # Pending approval waiters (approval_id -> Event)
        self._waiters: dict[str, asyncio.Event] = {}
        # Approval responses (approval_id -> response)
        self._responses: dict[str, dict[str, Any]] = {}

    def create_hook_matchers(self) -> dict[str, list[HookMatcher]]:
        """Create hook matchers for dangerous operations.

        Returns:
            Dict of HookEvent -> list[HookMatcher] for ClaudeAgentOptions.hooks
        """
        return {
            "PreToolUse": [
                # Bash commands - check for destructive patterns
                HookMatcher(
                    matcher="Bash",
                    hooks=[self._check_bash_command],
                    timeout=300.0,  # 5 minutes for human response
                ),
                # File operations - check for sensitive paths
                HookMatcher(
                    matcher="Write|Edit|MultiEdit",
                    hooks=[self._check_file_operation],
                    timeout=300.0,
                ),
                # Web requests - check for external APIs
                HookMatcher(
                    matcher="WebFetch",
                    hooks=[self._check_external_api],
                    timeout=300.0,
                ),
            ],
        }

    async def _check_bash_command(
        self,
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        """Hook callback for Bash commands.

        Checks if the command matches destructive patterns.
        """
        # Check if this is a PreToolUse event (TypedDict can't use isinstance)
        if hook_input.get("hook_event_name") != "PreToolUse":
            return SyncHookJSONOutput(continue_=True)

        tool_input = hook_input.get("tool_input", {})
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

        # Check against destructive patterns
        for pattern in DESTRUCTIVE_BASH_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                logger.warning(f"Destructive bash command detected: {command[:100]}")

                # Create approval request
                approval = await self._create_approval(
                    approval_type=ApprovalType.DESTRUCTIVE_COMMAND,
                    title=f"Destructive command: {command[:50]}",
                    summary=f"Agent wants to execute:\n\n```bash\n{command}\n```",
                    metadata={
                        "tool_name": "Bash",
                        "command": command,
                        "pattern_matched": pattern,
                    },
                )

                # Wait for human response
                response = await self._wait_for_approval(approval.id)

                if response.get("approved"):
                    return SyncHookJSONOutput(
                        continue_=True,
                        hookSpecificOutput={
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "permissionDecisionReason": f"Approved by {response.get('by', 'human')}",
                        },
                    )
                return SyncHookJSONOutput(
                    continue_=True,
                    hookSpecificOutput={
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": response.get("message", "Denied by human"),
                    },
                )

        # No dangerous pattern - allow
        return SyncHookJSONOutput(continue_=True)

    async def _check_file_operation(
        self,
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        """Hook callback for file operations.

        Checks if the file path matches sensitive patterns.
        """
        # Check if this is a PreToolUse event (TypedDict can't use isinstance)
        if hook_input.get("hook_event_name") != "PreToolUse":
            return SyncHookJSONOutput(continue_=True)

        tool_input = hook_input.get("tool_input", {})
        if not isinstance(tool_input, dict):
            return SyncHookJSONOutput(continue_=True)

        file_path = tool_input.get("file_path", "") or tool_input.get("path", "")

        # Check against sensitive file patterns
        for pattern in SENSITIVE_FILE_PATTERNS:
            if re.search(pattern, file_path, re.IGNORECASE):
                logger.warning(f"Sensitive file operation detected: {file_path}")

                # Create approval request
                approval = await self._create_approval(
                    approval_type=ApprovalType.SENSITIVE_FILE,
                    title=f"Sensitive file: {file_path}",
                    summary=f"Agent wants to modify a sensitive file:\n\n**File:** `{file_path}`",
                    metadata={
                        "tool_name": hook_input.get("tool_name", "unknown"),
                        "file_path": file_path,
                        "pattern_matched": pattern,
                        "content_preview": str(tool_input)[:500],
                    },
                )

                # Wait for human response
                response = await self._wait_for_approval(approval.id)

                if response.get("approved"):
                    return SyncHookJSONOutput(
                        continue_=True,
                        hookSpecificOutput={
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "permissionDecisionReason": f"Approved by {response.get('by', 'human')}",
                        },
                    )
                return SyncHookJSONOutput(
                    continue_=True,
                    hookSpecificOutput={
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": response.get("message", "Denied by human"),
                    },
                )

        # Not a sensitive file - allow
        return SyncHookJSONOutput(continue_=True)

    async def _check_external_api(
        self,
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        """Hook callback for WebFetch operations.

        Checks if the URL matches external API patterns that need approval.
        """
        # Check if this is a PreToolUse event (TypedDict can't use isinstance)
        if hook_input.get("hook_event_name") != "PreToolUse":
            return SyncHookJSONOutput(continue_=True)

        tool_input = hook_input.get("tool_input", {})
        url = tool_input.get("url", "") if isinstance(tool_input, dict) else ""

        # Check against external API patterns
        for pattern in EXTERNAL_API_APPROVAL_DOMAINS:
            if re.search(pattern, url, re.IGNORECASE):
                logger.warning(f"External API call detected: {url}")

                # Create approval request
                approval = await self._create_approval(
                    approval_type=ApprovalType.EXTERNAL_API,
                    title=f"External API: {url[:50]}",
                    summary=f"Agent wants to call an external API:\n\n**URL:** `{url}`",
                    metadata={
                        "tool_name": "WebFetch",
                        "url": url,
                        "pattern_matched": pattern,
                    },
                )

                # Wait for human response
                response = await self._wait_for_approval(approval.id)

                if response.get("approved"):
                    return SyncHookJSONOutput(
                        continue_=True,
                        hookSpecificOutput={
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "permissionDecisionReason": f"Approved by {response.get('by', 'human')}",
                        },
                    )
                return SyncHookJSONOutput(
                    continue_=True,
                    hookSpecificOutput={
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": response.get("message", "Denied by human"),
                    },
                )

        # Not an external API that needs approval - allow
        return SyncHookJSONOutput(continue_=True)

    async def _create_approval(
        self,
        approval_type: ApprovalType,
        title: str,
        summary: str,
        metadata: dict[str, Any],
    ) -> ApprovalRecord:
        """Create and persist an approval request.

        Args:
            approval_type: Type of approval needed
            title: Short description
            summary: Detailed context
            metadata: Type-specific data

        Returns:
            Created ApprovalRecord
        """
        timestamp = datetime.now(UTC).isoformat()
        approval_id = _generate_approval_id(
            self.agent_id, metadata.get("tool_name", "unknown"), timestamp
        )

        record = ApprovalRecord(
            id=approval_id,
            name=title[:100],  # Explicit name for Entity base class
            organization_id=self.org_id,
            project_id=self.project_id,
            agent_id=self.agent_id,
            task_id=self.task_id,
            approval_type=approval_type,
            title=title,
            summary=summary,
            metadata=metadata,
            status=ApprovalStatus.PENDING,
            expires_at=datetime.now(UTC) + DEFAULT_APPROVAL_TIMEOUT,
        )

        # Persist to graph
        await self.entity_manager.create(record)

        logger.info(f"Created approval request {approval_id}: {title}")
        return record

    async def _wait_for_approval(
        self, approval_id: str, wait_timeout: float = 300.0
    ) -> dict[str, Any]:
        """Wait for human response to an approval request.

        Args:
            approval_id: Approval record ID
            wait_timeout: Max wait time in seconds (default 5 minutes)

        Returns:
            Response dict with 'approved', 'by', 'message' keys
        """
        # Create event for this approval
        event = asyncio.Event()
        self._waiters[approval_id] = event

        try:
            # Wait for response or timeout
            await asyncio.wait_for(event.wait(), timeout=wait_timeout)
            return self._responses.pop(approval_id, {"approved": False, "message": "No response"})
        except TimeoutError:
            logger.warning(f"Approval {approval_id} timed out after {wait_timeout}s")
            # Update status to expired
            await self.entity_manager.update(approval_id, {"status": ApprovalStatus.EXPIRED.value})
            return {"approved": False, "message": "Approval request timed out"}
        finally:
            self._waiters.pop(approval_id, None)

    async def respond(
        self,
        approval_id: str,
        approved: bool,
        message: str = "",
        responded_by: str = "human",
    ) -> bool:
        """Respond to a pending approval request.

        Called by external API/UI when human makes a decision.

        Args:
            approval_id: Approval record ID
            approved: Whether to approve the operation
            message: Optional message to agent
            responded_by: User who responded

        Returns:
            True if response was accepted
        """
        # Update the record
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
        await self.entity_manager.update(
            approval_id,
            {
                "status": status.value,
                "responded_at": datetime.now(UTC).isoformat(),
                "response_by": responded_by,
                "response_message": message,
            },
        )

        # Store response for waiter
        self._responses[approval_id] = {
            "approved": approved,
            "by": responded_by,
            "message": message,
        }

        # Wake up waiter if present
        event = self._waiters.get(approval_id)
        if event:
            event.set()
            logger.info(f"Approval {approval_id} responded: {'approved' if approved else 'denied'}")
            return True
        logger.warning(f"No waiter found for approval {approval_id}")
        return False

    async def list_pending(self) -> list[ApprovalRecord]:
        """List all pending approval requests for this agent.

        Returns:
            List of pending ApprovalRecord objects
        """
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.APPROVAL,
            limit=100,
        )
        return [
            r
            for r in results
            if isinstance(r, ApprovalRecord)
            and r.agent_id == self.agent_id
            and r.status == ApprovalStatus.PENDING
        ]

    async def cancel_all(self, reason: str = "agent_stopped") -> int:
        """Cancel all pending approvals for this agent.

        Called when agent is stopped/terminated.

        Args:
            reason: Why approvals are being cancelled

        Returns:
            Number of approvals cancelled
        """
        pending = await self.list_pending()
        for approval in pending:
            await self.entity_manager.update(
                approval.id,
                {
                    "status": ApprovalStatus.EXPIRED.value,
                    "response_message": f"Cancelled: {reason}",
                },
            )
            # Wake up waiter with denial
            self._responses[approval.id] = {
                "approved": False,
                "message": f"Cancelled: {reason}",
            }
            event = self._waiters.get(approval.id)
            if event:
                event.set()

        return len(pending)
