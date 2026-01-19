"""ApprovalService for human-in-the-loop approvals.

Integrates with Claude Agent SDK's permission system via can_use_tool callback.
Routes permission requests through our UI for human approval.
"""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import structlog
from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    SyncHookJSONOutput,
    ToolPermissionContext,
)
from sqlalchemy import func, select

from sibyl.agents.state_sync import update_agent_state
from sibyl.db import get_session
from sibyl.db.models import AgentMessage, AgentMessageRole, AgentMessageType
from sibyl_core.models import (
    ApprovalRecord,
    ApprovalStatus,
    ApprovalType,
    EntityType,
)

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

log = structlog.get_logger()

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

# Default question timeout (shorter than approvals)
DEFAULT_QUESTION_TIMEOUT = timedelta(minutes=30)


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

    def create_can_use_tool_callback(
        self,
    ):
        """Create the can_use_tool callback for SDK permission integration.

        This is the proper way to integrate with the SDK's permission system.
        All permission requests go through this callback, which routes them
        to our approval UI.

        Returns:
            Async callback function for ClaudeAgentOptions.can_use_tool
        """

        async def can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            _context: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            """Handle SDK permission requests via our approval UI."""

            # Determine approval type and priority based on tool/input
            approval_type, title, summary, is_dangerous = self._classify_permission_request(
                tool_name, tool_input
            )

            # If not dangerous, auto-approve
            if not is_dangerous:
                log.debug(f"Auto-approving safe operation: {tool_name}")
                return PermissionResultAllow()

            log.info(f"Permission request requires approval: {tool_name}")

            # Create approval request and wait for human response
            approval = await self._create_approval(
                approval_type=approval_type,
                title=title,
                summary=summary,
                metadata={
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                },
            )

            response = await self._wait_for_approval(approval.id)

            if response.get("approved"):
                log.info(f"Permission approved by {response.get('by', 'human')}")
                return PermissionResultAllow()

            deny_reason = response.get("message") or "Permission denied by user"
            log.info(f"Permission denied: {deny_reason}")
            return PermissionResultDeny(message=deny_reason)

        return can_use_tool

    def _classify_permission_request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[ApprovalType, str, str, bool]:
        """Classify a permission request to determine approval handling.

        Returns:
            Tuple of (approval_type, title, summary, is_dangerous)
        """
        # Check for destructive bash commands
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            for pattern in DESTRUCTIVE_BASH_PATTERNS:
                if re.search(pattern, command, re.IGNORECASE):
                    return (
                        ApprovalType.DESTRUCTIVE_COMMAND,
                        f"⚠️ Destructive command: {command[:50]}",
                        f"Agent wants to execute:\n\n```bash\n{command}\n```",
                        True,
                    )

        # Check for sensitive file operations - ONLY sensitive files need approval
        if tool_name in ("Write", "Edit", "MultiEdit"):
            file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
            for pattern in SENSITIVE_FILE_PATTERNS:
                if re.search(pattern, file_path, re.IGNORECASE):
                    return (
                        ApprovalType.SENSITIVE_FILE,
                        f"⚠️ Sensitive file: {file_path}",
                        f"Agent wants to modify:\n\n**File:** `{file_path}`",
                        True,
                    )
            # Regular file writes - auto-approve (no approval needed)
            # Only sensitive files matching patterns above require human approval

        # Check for external API calls
        if tool_name == "WebFetch":
            url = tool_input.get("url", "")
            for pattern in EXTERNAL_API_APPROVAL_DOMAINS:
                if re.search(pattern, url, re.IGNORECASE):
                    return (
                        ApprovalType.EXTERNAL_API,
                        f"External API: {url[:50]}",
                        f"Agent wants to call:\n\n**URL:** `{url}`",
                        True,
                    )

        # Default: not dangerous, auto-approve
        return (ApprovalType.FILE_WRITE, "", "", False)

    def create_hook_matchers(self) -> dict[str, list[HookMatcher]]:
        """Create hook matchers for AskUserQuestion (questions still use hooks).

        Note: Permission requests now use can_use_tool callback instead of hooks.
        Only AskUserQuestion still needs a hook to intercept the tool.

        Returns:
            Dict of HookEvent -> list[HookMatcher] for ClaudeAgentOptions.hooks
        """
        return {
            "PreToolUse": [
                # AskUserQuestion - intercept and route through UI
                HookMatcher(
                    matcher="AskUserQuestion",
                    hooks=[self._handle_user_question],
                    timeout=1800.0,  # 30 minutes for user response
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
                log.warning(f"Destructive bash command detected: {command[:100]}")

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

        ALL file writes go through our approval UI. Sensitive files get
        flagged as higher priority.
        """
        # Check if this is a PreToolUse event (TypedDict can't use isinstance)
        if hook_input.get("hook_event_name") != "PreToolUse":
            return SyncHookJSONOutput(continue_=True)

        tool_input = hook_input.get("tool_input", {})
        if not isinstance(tool_input, dict):
            return SyncHookJSONOutput(continue_=True)

        tool_name = hook_input.get("tool_name", "unknown")
        file_path = tool_input.get("file_path", "") or tool_input.get("path", "")

        if not file_path:
            return SyncHookJSONOutput(continue_=True)

        # Check if this is a sensitive file (higher priority)
        is_sensitive = any(
            re.search(pattern, file_path, re.IGNORECASE) for pattern in SENSITIVE_FILE_PATTERNS
        )

        if is_sensitive:
            log.warning(f"Sensitive file operation detected: {file_path}")
            approval_type = ApprovalType.SENSITIVE_FILE
            title = f"⚠️ Sensitive file: {file_path}"
            summary = f"Agent wants to modify a **sensitive** file:\n\n**File:** `{file_path}`"
        else:
            log.info(f"File operation requires approval: {file_path}")
            approval_type = ApprovalType.FILE_WRITE
            title = f"File write: {file_path}"
            summary = f"Agent wants to write to:\n\n**File:** `{file_path}`"

        # Create approval request for ALL file operations
        approval = await self._create_approval(
            approval_type=approval_type,
            title=title,
            summary=summary,
            metadata={
                "tool_name": tool_name,
                "file_path": file_path,
                "is_sensitive": is_sensitive,
                "content_preview": str(tool_input.get("content", ""))[:500],
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
                log.warning(f"External API call detected: {url}")

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

    async def _handle_user_question(
        self,
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        """Hook callback for AskUserQuestion tool.

        Intercepts the tool, broadcasts to UI, waits for user response,
        and returns the answer to the agent.
        """
        if hook_input.get("hook_event_name") != "PreToolUse":
            return SyncHookJSONOutput(continue_=True)

        tool_input = hook_input.get("tool_input", {})
        if not isinstance(tool_input, dict):
            return SyncHookJSONOutput(continue_=True)

        questions = tool_input.get("questions", [])
        if not questions:
            return SyncHookJSONOutput(continue_=True)

        log.info(f"AskUserQuestion intercepted: {len(questions)} question(s)")

        # Create question record and broadcast
        question_id = await self._create_question(questions)

        # Wait for user response
        response = await self._wait_for_question_response(question_id)

        if response is None:
            # Timeout - return error to agent
            return SyncHookJSONOutput(
                continue_=True,
                hookSpecificOutput={
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "Question timed out waiting for user response",
                },
            )

        # Return the user's answers to the agent
        # We use the permissionDecisionReason to carry the answer JSON
        # The agent will parse this to get the user's choices
        import json as json_mod

        answers_json = json_mod.dumps(response.get("answers", {}))
        return SyncHookJSONOutput(
            continue_=True,
            hookSpecificOutput={
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": f"User answered: {answers_json}",
            },
        )

    async def _create_question(self, questions: list[dict[str, Any]]) -> str:
        """Create and broadcast a user question request.

        Args:
            questions: List of question dicts from AskUserQuestion tool

        Returns:
            Question ID for tracking response
        """
        timestamp = datetime.now(UTC).isoformat()
        expires_at = datetime.now(UTC) + DEFAULT_QUESTION_TIMEOUT
        question_id = _generate_approval_id(self.agent_id, "AskUserQuestion", timestamp)

        # Build message payload
        message_payload = {
            "agent_id": self.agent_id,
            "message_type": "user_question",
            "question_id": question_id,
            "questions": questions,
            "expires_at": expires_at.isoformat(),
            "status": "pending",
        }

        # Store to database for persistence
        try:
            async with get_session() as session:
                # Get next message_num for this agent
                result = await session.execute(
                    select(func.coalesce(func.max(AgentMessage.message_num), 0)).where(  # type: ignore[arg-type]
                        AgentMessage.agent_id == self.agent_id
                    )
                )
                message_num = (result.scalar() or 0) + 1

                # Build content from questions
                content_parts = ["🤔 **Question for you:**"]
                content_parts.extend(
                    f"\n**{q.get('header', 'Question')}:** {q.get('question', '')}"
                    for q in questions
                )

                msg = AgentMessage(
                    agent_id=self.agent_id,
                    organization_id=UUID(self.org_id),
                    message_num=message_num,
                    role=AgentMessageRole.system,
                    type=AgentMessageType.text,
                    content="\n".join(content_parts),
                    extra=message_payload,
                )
                session.add(msg)
                await session.commit()
                message_payload["message_num"] = message_num
        except Exception as e:
            log.warning(f"Failed to store question message: {e}")

        # Broadcast via WebSocket
        try:
            from sibyl.api.pubsub import publish_event

            await publish_event("agent_message", message_payload, org_id=self.org_id)

            # Also broadcast agent status change
            await publish_event(
                "agent_status",
                {"agent_id": self.agent_id, "status": "waiting_input"},
                org_id=self.org_id,
            )
        except Exception as e:
            log.warning(f"Failed to broadcast question: {e}")

        log.info(f"Created user question {question_id}")
        return question_id

    async def _wait_for_question_response(
        self, question_id: str, wait_timeout: float = 1800.0
    ) -> dict[str, Any] | None:
        """Wait for user response to a question via Redis.

        Args:
            question_id: Question ID
            wait_timeout: Max wait time in seconds (default 30 minutes)

        Returns:
            Response dict with 'answers' key, or None if timeout
        """
        from sibyl.agents.redis_sub import wait_for_question_response

        response = await wait_for_question_response(question_id, wait_timeout=wait_timeout)

        if response is None:
            log.warning(f"Question {question_id} timed out after {wait_timeout}s")
            return None

        return response

    async def _create_approval(
        self,
        approval_type: ApprovalType,
        title: str,
        summary: str,
        metadata: dict[str, Any],
    ) -> ApprovalRecord:
        """Create and persist an approval request.

        Also updates agent status and broadcasts to UI for real-time display.

        Args:
            approval_type: Type of approval needed
            title: Short description
            summary: Detailed context
            metadata: Type-specific data

        Returns:
            Created ApprovalRecord
        """
        timestamp = datetime.now(UTC).isoformat()
        expires_at = datetime.now(UTC) + DEFAULT_APPROVAL_TIMEOUT
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
            expires_at=expires_at,
        )

        # Persist to graph using fast direct insert (no LLM extraction needed)
        await self.entity_manager.create_direct(record, generate_embedding=False)

        # Update agent status to waiting_approval
        from sibyl_core.models import AgentStatus

        await self.entity_manager.update(
            self.agent_id,
            {"status": AgentStatus.WAITING_APPROVAL.value},
        )
        await update_agent_state(
            org_id=self.org_id,
            agent_id=self.agent_id,
            status=AgentStatus.WAITING_APPROVAL.value,
        )

        # Broadcast approval request to UI via WebSocket
        await self._broadcast_approval_request(record, expires_at)

        log.info(f"Created approval request {approval_id}: {title}")
        return record

    async def _broadcast_approval_request(
        self,
        record: ApprovalRecord,
        expires_at: datetime,
    ) -> None:
        """Broadcast approval request to UI and persist to database."""
        # Build message payload
        message_payload = {
            "agent_id": self.agent_id,
            "message_type": "approval_request",
            "approval_id": record.id,
            "approval_type": record.approval_type.value,
            "title": record.title,
            "summary": record.summary,
            "metadata": record.metadata,
            "actions": ["approve", "deny"],
            "expires_at": expires_at.isoformat(),
            "status": "pending",
        }

        # Store to database for persistence across page reloads
        try:
            async with get_session() as session:
                # Get next message_num for this agent
                result = await session.execute(
                    select(func.coalesce(func.max(AgentMessage.message_num), 0)).where(  # type: ignore[arg-type]
                        AgentMessage.agent_id == self.agent_id
                    )
                )
                message_num = (result.scalar() or 0) + 1

                msg = AgentMessage(
                    agent_id=self.agent_id,
                    organization_id=UUID(self.org_id),
                    message_num=message_num,
                    role=AgentMessageRole.system,
                    type=AgentMessageType.text,  # Use text type, metadata indicates approval
                    content=f"🔐 **Approval Required:** {record.title}",
                    extra=message_payload,  # Full approval data in JSONB
                )
                session.add(msg)
                await session.commit()
                message_payload["message_num"] = message_num
        except Exception as e:
            log.warning(f"Failed to store approval message: {e}")

        # Broadcast via WebSocket for real-time display
        try:
            from sibyl.api.pubsub import publish_event

            await publish_event("agent_message", message_payload, org_id=self.org_id)

            # Also broadcast agent status change
            await publish_event(
                "agent_status",
                {"agent_id": self.agent_id, "status": "waiting_approval"},
                org_id=self.org_id,
            )
        except Exception as e:
            log.warning(f"Failed to broadcast approval request: {e}")

    async def _wait_for_approval(
        self, approval_id: str, wait_timeout: float = 300.0
    ) -> dict[str, Any]:
        """Wait for human response to an approval request via Redis.

        Uses Redis pubsub for cross-process communication since worker
        and API run in separate processes and can't share memory.

        Args:
            approval_id: Approval record ID
            wait_timeout: Max wait time in seconds (default 5 minutes)

        Returns:
            Response dict with 'approved', 'by', 'message' keys
        """
        from sibyl.agents.redis_sub import wait_for_approval_response

        # Wait for response via Redis pubsub
        response = await wait_for_approval_response(approval_id, wait_timeout=wait_timeout)

        if response is None:
            # Timeout
            log.warning(f"Approval {approval_id} timed out after {wait_timeout}s")
            await self.entity_manager.update(approval_id, {"status": ApprovalStatus.EXPIRED.value})
            return {"approved": False, "message": "Approval request timed out"}

        return response

    async def respond(
        self,
        approval_id: str,
        approved: bool,
        message: str = "",
        responded_by: str = "human",
    ) -> bool:
        """Respond to a pending approval request.

        DEPRECATED: This method only updates the graph record.
        Use the API route which also publishes to Redis for the worker.

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

        log.info(f"Approval {approval_id} responded: {'approved' if approved else 'denied'}")
        return True

    async def list_pending(self) -> list[ApprovalRecord]:
        """List all pending approval requests for this agent.

        Returns:
            List of pending ApprovalRecord objects
        """
        results = await self.entity_manager.list_by_type(
            entity_type=EntityType.APPROVAL,
            limit=100,
        )
        pending: list[ApprovalRecord] = []
        for record in results:
            if record.entity_type != EntityType.APPROVAL:
                continue
            approval = cast("ApprovalRecord", record)
            if approval.agent_id == self.agent_id and approval.status == ApprovalStatus.PENDING:
                pending.append(approval)
        return pending

    async def cancel_all(self, reason: str = "agent_stopped") -> int:
        """Cancel all pending approvals for this agent.

        Called when agent is stopped/terminated. Publishes denial
        via Redis so any waiting _wait_for_approval calls receive it.

        Args:
            reason: Why approvals are being cancelled

        Returns:
            Number of approvals cancelled
        """
        from sibyl.agents.redis_sub import publish_approval_response

        pending = await self.list_pending()
        for approval in pending:
            await self.entity_manager.update(
                approval.id,
                {
                    "status": ApprovalStatus.EXPIRED.value,
                    "response_message": f"Cancelled: {reason}",
                },
            )
            # Publish denial via Redis so worker receives it
            await publish_approval_response(
                approval.id,
                {
                    "approved": False,
                    "message": f"Cancelled: {reason}",
                    "by": "system",
                },
            )

        return len(pending)
