"""ApprovalQueue for managing blocking approval waits with recovery.

Provides a queue-based abstraction over the approval workflow with:
- Blocking waits with configurable timeout
- Process restart recovery via reattach_waiter()
- Auto-expiration of stale requests
- Redis-backed persistence for durability

This sits on top of ApprovalService and redis_sub, adding recovery semantics.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from redis.asyncio import Redis

from sibyl.agents.redis_sub import (
    APPROVAL_CHANNEL_PREFIX,
    PUBSUB_DB,
    publish_approval_response,
)
from sibyl.config import settings
from sibyl_core.models import ApprovalRecord, ApprovalStatus, ApprovalType

if TYPE_CHECKING:
    from sibyl_core.graph import EntityManager

log = structlog.get_logger()

# Redis key for storing pending approval metadata (for recovery)
PENDING_APPROVALS_KEY = "sibyl:pending_approvals"

# How long to keep approval state in Redis (should exceed max timeout)
APPROVAL_STATE_TTL = timedelta(hours=48)


class ApprovalQueue:
    """Queue-based approval management with recovery support.

    Unlike the lower-level ApprovalService, this class:
    - Stores approval state in Redis for recovery after restart
    - Allows reattachment to pending approvals
    - Handles expiration more gracefully
    """

    def __init__(
        self,
        entity_manager: "EntityManager",
        org_id: str,
        project_id: str,
        agent_id: str,
        task_id: str | None = None,
    ):
        """Initialize ApprovalQueue.

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
        self._redis: Redis | None = None

    async def _get_redis(self) -> Redis:
        """Get or create Redis connection."""
        if self._redis is None or not await self._ping_redis():
            self._redis = Redis(
                host=settings.falkordb_host,
                port=settings.falkordb_port,
                password=settings.falkordb_password,
                db=PUBSUB_DB,
                decode_responses=True,
            )
        return self._redis

    async def _ping_redis(self) -> bool:
        """Check if Redis connection is alive."""
        if self._redis is None:
            return False
        try:
            await self._redis.ping()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    async def enqueue(
        self,
        approval_type: ApprovalType,
        title: str,
        summary: str,
        metadata: dict[str, Any],
        expiry: timedelta = timedelta(hours=24),
    ) -> ApprovalRecord:
        """Enqueue an approval request.

        Creates the approval record, stores recovery state in Redis,
        and broadcasts to UI.

        Args:
            approval_type: Type of approval needed
            title: Short description
            summary: Detailed context
            metadata: Type-specific data
            expiry: How long before auto-expiration

        Returns:
            Created ApprovalRecord
        """
        from sibyl.agents.approvals import _generate_approval_id

        timestamp = datetime.now(UTC).isoformat()
        expires_at = datetime.now(UTC) + expiry
        approval_id = _generate_approval_id(
            self.agent_id, metadata.get("tool_name", "unknown"), timestamp
        )

        record = ApprovalRecord(
            id=approval_id,
            name=title[:100],
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

        # Persist to graph
        await self.entity_manager.create_direct(record, generate_embedding=False)

        # Store recovery state in Redis
        await self._store_pending_state(record, expires_at)

        # Update agent status
        from sibyl_core.models import AgentStatus

        await self.entity_manager.update(
            self.agent_id,
            {"status": AgentStatus.WAITING_APPROVAL.value},
        )

        # Broadcast to UI
        await self._broadcast_approval_request(record, expires_at)

        log.info(
            "Enqueued approval request",
            approval_id=approval_id,
            type=approval_type.value,
            expires_at=expires_at.isoformat(),
        )

        return record

    async def wait_for_response(
        self,
        approval_id: str,
        wait_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Wait for response to an approval request.

        Blocks until human responds or timeout expires. If timeout expires,
        the approval is marked as expired.

        Args:
            approval_id: Approval record ID
            wait_seconds: Max wait time in seconds

        Returns:
            Response dict with 'approved', 'by', 'message' keys
        """
        channel = f"{APPROVAL_CHANNEL_PREFIX}{approval_id}"
        redis = await self._get_redis()
        pubsub = redis.pubsub()

        try:
            # Check if already responded (recovery case)
            existing = await self._check_existing_response(approval_id)
            if existing is not None:
                log.info(
                    "Found existing response for approval",
                    approval_id=approval_id,
                    approved=existing.get("approved"),
                )
                return existing

            # Subscribe and wait
            await pubsub.subscribe(channel)
            log.debug("Waiting for approval response", approval_id=approval_id)

            async with asyncio.timeout(wait_seconds):
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        data = json.loads(message["data"])
                        log.info(
                            "Received approval response",
                            approval_id=approval_id,
                            approved=data.get("approved"),
                        )
                        # Clear pending state
                        await self._clear_pending_state(approval_id)
                        return data

        except TimeoutError:
            log.warning("Approval timed out", approval_id=approval_id)
            await self._handle_timeout(approval_id)
            return {"approved": False, "message": "Approval request timed out", "by": "system"}

        except Exception as e:
            log.exception("Error waiting for approval", approval_id=approval_id, error=str(e))
            return {"approved": False, "message": f"Error: {e}", "by": "system"}

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

        return {"approved": False, "message": "Unexpected end of wait", "by": "system"}

    async def respond(
        self,
        approval_id: str,
        approved: bool,
        message: str = "",
        responded_by: str = "human",
    ) -> bool:
        """Respond to a pending approval.

        Updates the graph record, clears pending state, and publishes
        response to waiting worker.

        Args:
            approval_id: Approval record ID
            approved: Whether to approve
            message: Optional message
            responded_by: Who responded

        Returns:
            True if response was processed
        """
        # Update graph record
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

        # Store response in Redis for recovery
        await self._store_response(approval_id, approved, message, responded_by)

        # Publish to waiting worker
        await publish_approval_response(
            approval_id,
            {
                "approved": approved,
                "message": message,
                "by": responded_by,
            },
        )

        # Clear pending state
        await self._clear_pending_state(approval_id)

        log.info(
            "Responded to approval",
            approval_id=approval_id,
            approved=approved,
            by=responded_by,
        )

        return True

    async def reattach_waiter(
        self,
        approval_id: str,
        wait_seconds: float = 300.0,
    ) -> dict[str, Any] | None:
        """Reattach to a pending approval after process restart.

        Checks if approval was already responded while we were down.
        If not, continues waiting. If already responded, returns the response.

        Args:
            approval_id: Approval record ID to reattach to
            wait_seconds: Remaining wait time in seconds

        Returns:
            Response dict if already responded or when received,
            None if approval not found or invalid
        """
        # First check if we were waiting on this approval
        pending = await self._get_pending_state(approval_id)
        if pending is None:
            log.warning("No pending state found for reattach", approval_id=approval_id)
            return None

        # Check if response was stored while we were down
        existing = await self._check_existing_response(approval_id)
        if existing is not None:
            log.info(
                "Found response during reattach",
                approval_id=approval_id,
                approved=existing.get("approved"),
            )
            await self._clear_pending_state(approval_id)
            return existing

        # Check if approval expired
        expires_at_str = pending.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if datetime.now(UTC) > expires_at:
                log.warning("Approval expired during downtime", approval_id=approval_id)
                await self._handle_timeout(approval_id)
                return {"approved": False, "message": "Expired during downtime", "by": "system"}

            # Adjust wait time to remaining time
            remaining = (expires_at - datetime.now(UTC)).total_seconds()
            wait_seconds = min(wait_seconds, remaining)

        # Continue waiting
        log.info("Reattaching to approval wait", approval_id=approval_id, wait_seconds=wait_seconds)
        return await self.wait_for_response(approval_id, wait_seconds=wait_seconds)

    async def list_pending(self) -> list[dict[str, Any]]:
        """List all pending approvals for this agent.

        Returns pending approvals from Redis state (faster than graph query).

        Returns:
            List of pending approval dicts
        """
        redis = await self._get_redis()
        pattern = f"{PENDING_APPROVALS_KEY}:{self.agent_id}:*"

        pending = []
        async for key in redis.scan_iter(pattern):
            data = await redis.get(key)
            if data:
                pending.append(json.loads(data))

        return pending

    async def cancel_all(self, reason: str = "agent_stopped") -> int:
        """Cancel all pending approvals for this agent.

        Args:
            reason: Why approvals are cancelled

        Returns:
            Number of approvals cancelled
        """
        pending = await self.list_pending()

        for approval_data in pending:
            approval_id = approval_data.get("id")
            if approval_id:
                await self.respond(
                    approval_id,
                    approved=False,
                    message=f"Cancelled: {reason}",
                    responded_by="system",
                )

        return len(pending)

    async def expire_stale(self, max_age: timedelta | None = None) -> int:
        """Expire stale approval requests.

        Checks all pending approvals and expires those past their deadline.
        Should be called periodically by a background task.

        Args:
            max_age: Optional max age override (uses stored expires_at otherwise)

        Returns:
            Number of approvals expired
        """
        redis = await self._get_redis()
        pattern = f"{PENDING_APPROVALS_KEY}:{self.agent_id}:*"
        now = datetime.now(UTC)
        expired_count = 0

        async for key in redis.scan_iter(pattern):
            data = await redis.get(key)
            if not data:
                continue

            approval_data = json.loads(data)
            expires_at_str = approval_data.get("expires_at")

            if expires_at_str:
                expires_at = datetime.fromisoformat(expires_at_str)
                if now > expires_at:
                    approval_id = approval_data.get("id")
                    if approval_id:
                        await self._handle_timeout(approval_id)
                        expired_count += 1
            elif max_age:
                created_at_str = approval_data.get("created_at")
                if created_at_str:
                    created_at = datetime.fromisoformat(created_at_str)
                    if now - created_at > max_age:
                        approval_id = approval_data.get("id")
                        if approval_id:
                            await self._handle_timeout(approval_id)
                            expired_count += 1

        return expired_count

    # --- Private helpers ---

    async def _store_pending_state(
        self,
        record: ApprovalRecord,
        expires_at: datetime,
    ) -> None:
        """Store pending approval state in Redis for recovery."""
        redis = await self._get_redis()
        key = f"{PENDING_APPROVALS_KEY}:{self.agent_id}:{record.id}"

        state = {
            "id": record.id,
            "agent_id": self.agent_id,
            "org_id": self.org_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "approval_type": record.approval_type.value,
            "title": record.title,
            "summary": record.summary,
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": expires_at.isoformat(),
            "status": "pending",
        }

        await redis.setex(
            key,
            int(APPROVAL_STATE_TTL.total_seconds()),
            json.dumps(state),
        )

    async def _get_pending_state(self, approval_id: str) -> dict[str, Any] | None:
        """Get pending approval state from Redis."""
        redis = await self._get_redis()
        key = f"{PENDING_APPROVALS_KEY}:{self.agent_id}:{approval_id}"
        data = await redis.get(key)
        return json.loads(data) if data else None

    async def _clear_pending_state(self, approval_id: str) -> None:
        """Clear pending approval state from Redis."""
        redis = await self._get_redis()
        key = f"{PENDING_APPROVALS_KEY}:{self.agent_id}:{approval_id}"
        await redis.delete(key)

    async def _store_response(
        self,
        approval_id: str,
        approved: bool,
        message: str,
        responded_by: str,
    ) -> None:
        """Store approval response in Redis for recovery.

        If worker crashes, it can find the response on restart.
        """
        redis = await self._get_redis()
        key = f"sibyl:approval_response:{approval_id}"

        response = {
            "approved": approved,
            "message": message,
            "by": responded_by,
            "responded_at": datetime.now(UTC).isoformat(),
        }

        # Store with TTL - response only needed for recovery window
        await redis.setex(
            key,
            int(APPROVAL_STATE_TTL.total_seconds()),
            json.dumps(response),
        )

    async def _check_existing_response(self, approval_id: str) -> dict[str, Any] | None:
        """Check if approval already has a response in Redis."""
        redis = await self._get_redis()
        key = f"sibyl:approval_response:{approval_id}"
        data = await redis.get(key)

        if data:
            return json.loads(data)

        # Also check graph record as fallback
        try:
            entity = await self.entity_manager.get(approval_id)
            if isinstance(entity, ApprovalRecord):
                if entity.status in (ApprovalStatus.APPROVED, ApprovalStatus.DENIED):
                    return {
                        "approved": entity.status == ApprovalStatus.APPROVED,
                        "message": getattr(entity, "response_message", ""),
                        "by": getattr(entity, "response_by", "unknown"),
                    }
        except Exception as e:
            log.debug(
                "Could not check graph for existing response", approval_id=approval_id, error=str(e)
            )

        return None

    async def _handle_timeout(self, approval_id: str) -> None:
        """Handle approval timeout - update graph and clear state."""
        try:
            await self.entity_manager.update(
                approval_id,
                {
                    "status": ApprovalStatus.EXPIRED.value,
                    "response_message": "Timed out waiting for response",
                },
            )
        except Exception as e:
            log.warning("Failed to update expired approval", approval_id=approval_id, error=str(e))

        await self._clear_pending_state(approval_id)

        # Publish timeout response so any waiting process receives it
        await publish_approval_response(
            approval_id,
            {
                "approved": False,
                "message": "Request timed out",
                "by": "system",
            },
        )

    async def _broadcast_approval_request(
        self,
        record: ApprovalRecord,
        expires_at: datetime,
    ) -> None:
        """Broadcast approval request to UI via WebSocket."""
        from uuid import UUID

        from sqlalchemy import func, select

        from sibyl.db import get_session
        from sibyl.db.models import AgentMessage, AgentMessageRole, AgentMessageType

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

        # Store to database
        try:
            async with get_session() as session:
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
                    type=AgentMessageType.text,
                    content=f"🔐 **Approval Required:** {record.title}",
                    extra=message_payload,
                )
                session.add(msg)
                await session.commit()
                message_payload["message_num"] = message_num
        except Exception as e:
            log.warning("Failed to store approval message", error=str(e))

        # Broadcast via WebSocket
        try:
            from sibyl.api.pubsub import publish_event

            await publish_event("agent_message", message_payload, org_id=self.org_id)
            await publish_event(
                "agent_status",
                {"agent_id": self.agent_id, "status": "waiting_approval"},
                org_id=self.org_id,
            )
        except Exception as e:
            log.warning("Failed to broadcast approval request", error=str(e))


async def create_approval_queue(
    entity_manager: "EntityManager",
    org_id: str,
    project_id: str,
    agent_id: str,
    task_id: str | None = None,
) -> ApprovalQueue:
    """Factory function for creating ApprovalQueue instances.

    Args:
        entity_manager: Graph client
        org_id: Organization UUID
        project_id: Project UUID
        agent_id: Agent UUID
        task_id: Optional task UUID

    Returns:
        Configured ApprovalQueue instance
    """
    return ApprovalQueue(
        entity_manager=entity_manager,
        org_id=org_id,
        project_id=project_id,
        agent_id=agent_id,
        task_id=task_id,
    )
