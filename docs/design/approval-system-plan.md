# Approval System Implementation Plan

## Problem Statement

When an agent hits a permission issue (destructive command, sensitive file, external API), it
should:

1. Pause execution and request human approval
2. Show the approval request inline in the chat thread
3. Allow user to approve/deny directly in the thread
4. If not handled in-thread, show in approval queue with notification
5. Resume execution after human decision

## Current State Analysis

### What Exists

**ApprovalService** (`apps/api/src/sibyl/agents/approvals.py`):

- Hook matchers for PreToolUse events
- Detects: destructive bash commands, sensitive files, external APIs
- Creates ApprovalRecord entities in graph
- Has `_wait_for_approval()` using asyncio.Event
- Has `respond()` method to wake up waiters

**API Routes** (`apps/api/src/sibyl/api/routes/approvals.py`):

- GET /approvals - list with filters
- GET /approvals/pending - convenience endpoint
- POST /approvals/{id}/respond - respond to approval
- Publishes `approval_response` event via pubsub

**Frontend**:

- `approval-queue.tsx` - exists but may need updates
- Some hooks in `hooks.ts`

### Critical Bug: Process Isolation

```
Worker Process (runs agent)          API Process (handles HTTP)
├── AgentRunner                      ├── FastAPI routes
├── ApprovalService                  ├── respond_to_approval()
│   ├── _waiters: dict[id, Event]    │   └── Updates graph
│   └── _wait_for_approval()         │   └── Publishes event
└── Blocked on asyncio.Event         └── CAN'T ACCESS _waiters!
```

The `respond_to_approval` API updates the graph and publishes an event, but **never signals the
asyncio.Event** in the worker process. They don't share memory.

## Agent SDK Hook Behavior (Key Insights)

From exhaustive SDK research:

1. **Hooks are async but BLOCKING** - Agent waits for hook completion (up to timeout)
2. **No built-in "waiting_approval" status** - Agent internally paused, we must track ourselves
3. **Default timeout 60s** - We use 300s (5 min) for human approval
4. **Python lacks async cancellation** - context.signal is always None, must handle manually
5. **Permission decisions**: Return `permissionDecision: 'allow'|'deny'|'ask'` in hookSpecificOutput
6. **Hook chain**: First deny stops chain, multiple hooks execute in order

## Solution Architecture

### Dual-Channel Redis Communication

**Critical insight**: Current `pubsub.py` is WebSocket fan-out only—it broadcasts to browser clients
via SSE/WebSocket, not to worker processes. Workers need **direct Redis subscription**.

Two separate channels:

1. **Worker channel**: `sibyl:approval:{id}` — API publishes approval response, worker subscribes
   directly via aioredis
2. **WebSocket channel**: `sibyl:websocket:events` — Worker broadcasts approval_request for UI
   display

```
1. PreToolUse hook triggers dangerous pattern
2. ApprovalService creates ApprovalRecord in graph
3. ApprovalService subscribes DIRECTLY to Redis: `sibyl:approval:{approval_id}`
4. ApprovalService blocks on asyncio.Event (up to 5 min)
5. Worker broadcasts `approval_request` to WebSocket channel (for UI)

--- Human sees approval in thread or queue ---

6. User clicks Approve/Deny in UI
7. API route updates graph status
8. API publishes to Redis: `sibyl:approval:{approval_id}` (worker channel)
9. Worker's direct Redis subscription receives message
10. Worker sets asyncio.Event with response
11. Hook returns permissionDecision to agent
12. Agent continues execution
```

## Implementation Phases

### Phase 1: Fix Worker-API Communication

**1.1 Add direct Redis subscription to ApprovalService**

The worker needs its own Redis subscription (not the WebSocket pubsub). Create a helper module:

```python
# In agents/redis_sub.py (NEW)

import asyncio
import json
from typing import Callable, Any
import redis.asyncio as aioredis
from sibyl.config import settings

_redis: aioredis.Redis | None = None

async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(settings.redis_url)
    return _redis

async def wait_for_message(
    channel: str,
    timeout: float = 300.0,
) -> dict[str, Any] | None:
    """Subscribe to channel and wait for single message."""
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    try:
        async with asyncio.timeout(timeout):
            async for message in pubsub.listen():
                if message["type"] == "message":
                    return json.loads(message["data"])
    except TimeoutError:
        return None
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()
```

```python
# In approvals.py

async def _wait_for_approval(
    self, approval_id: str, wait_timeout: float = 300.0
) -> dict[str, Any]:
    """Wait for human response via direct Redis subscription."""
    from sibyl.agents.redis_sub import wait_for_message

    channel = f"sibyl:approval:{approval_id}"

    # Direct Redis subscription - not through WebSocket pubsub
    response = await wait_for_message(channel, timeout=wait_timeout)

    if response is None:
        # Timeout - update status
        await self.entity_manager.update(approval_id, {"status": "expired"})
        return {"approved": False, "message": "Approval request timed out"}

    return response
```

**1.2 Update respond_to_approval API to publish to worker channel**

```python
# In routes/approvals.py

import redis.asyncio as aioredis
import json

@router.post("/{approval_id}/respond")
async def respond_to_approval(...):
    # ... existing graph update code ...

    # Publish DIRECTLY to Redis (worker is subscribed via redis_sub.py)
    redis = await aioredis.from_url(settings.redis_url)
    await redis.publish(
        f"sibyl:approval:{approval_id}",  # Worker listens on this channel
        json.dumps({
            "approved": request.action == "approve",
            "action": request.action,
            "by": str(user.id),
            "message": request.message,
        }),
    )
    await redis.close()

    # ALSO broadcast to WebSocket for UI status update
    await publish_event(
        "approval_response",
        {"approval_id": approval_id, "action": request.action},
        org_id=str(org.id),
    )
```

### Phase 2: Agent Status Updates

**2.1 Update agent status when approval created**

```python
# In approvals.py _create_approval()

async def _create_approval(self, ...):
    # ... create record ...

    # Update agent status
    await self.entity_manager.update(
        self.agent_id,
        {"status": AgentStatus.WAITING_APPROVAL.value}
    )

    # Broadcast status change
    await _safe_broadcast(
        "agent_status",
        {"agent_id": self.agent_id, "status": "waiting_approval"},
        org_id=self.org_id,
    )

    return record
```

**2.2 Update agent status when approval resolved**

```python
# In approvals.py respond()

async def respond(self, ...):
    # ... existing code ...

    # Update agent status back to working
    await self.entity_manager.update(
        self.agent_id,
        {"status": AgentStatus.WORKING.value}
    )

    await _safe_broadcast(
        "agent_status",
        {"agent_id": self.agent_id, "status": "working"},
        org_id=self.org_id,
    )
```

### Phase 3: Broadcast Approval Request to UI

**3.1 Broadcast approval_request message from worker**

```python
# In approvals.py _create_approval()

# After creating record, broadcast to UI
await _safe_broadcast(
    "agent_message",
    {
        "agent_id": self.agent_id,
        "message_type": "approval_request",
        "approval_id": approval_id,
        "approval_type": approval_type.value,
        "title": title,
        "summary": summary,
        "actions": ["approve", "deny"],
        "metadata": metadata,
        "expires_at": (datetime.now(UTC) + DEFAULT_APPROVAL_TIMEOUT).isoformat(),
    },
    org_id=self.org_id,
)
```

**3.2 Store approval_request as AgentMessage**

```python
# In worker.py _store_agent_message() - handle approval_request type
# This allows reload to show the approval inline
```

### Phase 4: In-Thread Approval UI

**4.1 New ApprovalRequestMessage component**

```tsx
// apps/web/src/components/agents/approval-request-message.tsx

interface ApprovalRequestMessageProps {
  approvalId: string;
  approvalType: string;
  title: string;
  summary: string;
  actions: string[];
  metadata?: Record<string, unknown>;
  expiresAt?: string;
  status?: "pending" | "approved" | "denied" | "expired";
}

export function ApprovalRequestMessage({
  approvalId,
  approvalType,
  title,
  summary,
  actions,
  metadata,
  expiresAt,
  status = "pending",
}: ApprovalRequestMessageProps) {
  const respondMutation = useRespondToApproval();
  const [isResponding, setIsResponding] = useState(false);

  const handleAction = async (action: string) => {
    setIsResponding(true);
    await respondMutation.mutateAsync({ approvalId, action });
    setIsResponding(false);
  };

  const isPending = status === "pending";
  const isExpired = expiresAt && new Date(expiresAt) < new Date();

  return (
    <div
      className={`rounded-lg border p-4 ${
        isPending
          ? "border-sc-yellow/50 bg-sc-yellow/5"
          : status === "approved"
            ? "border-sc-green/30 bg-sc-green/5"
            : "border-sc-red/30 bg-sc-red/5"
      }`}
    >
      {/* Header with type badge */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-sc-yellow">⚠️</span>
        <span className="text-xs px-2 py-0.5 rounded bg-sc-yellow/20 text-sc-yellow">
          {approvalType.replace("_", " ")}
        </span>
        <span className="text-sm font-medium text-sc-fg-primary">{title}</span>
      </div>

      {/* Summary with markdown */}
      <div className="mb-4">
        <Markdown content={summary} className="text-sm" />
      </div>

      {/* Metadata (command, file path, etc.) */}
      {metadata && (
        <div className="mb-4 p-2 bg-sc-bg-dark rounded text-xs font-mono">
          {metadata.command && <div>$ {metadata.command}</div>}
          {metadata.file_path && <div>File: {metadata.file_path}</div>}
          {metadata.url && <div>URL: {metadata.url}</div>}
        </div>
      )}

      {/* Actions */}
      {isPending && !isExpired ? (
        <div className="flex gap-2">
          <button
            onClick={() => handleAction("approve")}
            disabled={isResponding}
            className="px-4 py-2 bg-sc-green text-white rounded hover:bg-sc-green/80"
          >
            {isResponding ? <Spinner size="sm" /> : "Approve"}
          </button>
          <button
            onClick={() => handleAction("deny")}
            disabled={isResponding}
            className="px-4 py-2 bg-sc-red text-white rounded hover:bg-sc-red/80"
          >
            {isResponding ? <Spinner size="sm" /> : "Deny"}
          </button>
        </div>
      ) : (
        <div className="text-sm text-sc-fg-muted">
          {status === "approved" && "✓ Approved"}
          {status === "denied" && "✗ Denied"}
          {status === "expired" && "⏱ Expired"}
        </div>
      )}

      {/* Expiry countdown */}
      {isPending && expiresAt && (
        <div className="mt-2 text-xs text-sc-fg-subtle">
          Expires: <TimeAgo date={expiresAt} />
        </div>
      )}
    </div>
  );
}
```

**4.2 Update agent-chat-panel.tsx to render approvals**

```tsx
// In ChatMessageComponent or groupMessages

// Detect approval_request messages
if (message.type === "approval_request" || message.metadata?.message_type === "approval_request") {
  return (
    <ApprovalRequestMessage
      approvalId={message.metadata.approval_id}
      approvalType={message.metadata.approval_type}
      title={message.metadata.title}
      summary={message.metadata.summary}
      actions={message.metadata.actions}
      metadata={message.metadata.metadata}
      expiresAt={message.metadata.expires_at}
      status={message.metadata.status}
    />
  );
}
```

**4.3 Add useRespondToApproval hook**

```tsx
// In lib/hooks.ts

export function useRespondToApproval() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      approvalId,
      action,
      message,
    }: {
      approvalId: string;
      action: string;
      message?: string;
    }) => {
      const response = await api.post(`/api/approvals/${approvalId}/respond`, {
        action,
        message,
      });
      return response.data;
    },
    onSuccess: (_, { approvalId }) => {
      // Invalidate queries
      queryClient.invalidateQueries({ queryKey: ["approvals"] });
      queryClient.invalidateQueries({ queryKey: ["approval", approvalId] });
    },
  });
}
```

### Phase 5: Notification System

**5.1 Add pending approval count endpoint**

```python
# In routes/approvals.py

@router.get("/count/pending")
async def get_pending_count(
    org: Organization = Depends(get_current_organization),
) -> dict[str, int]:
    """Get count of pending approvals for notification badge."""
    # ... query logic ...
    return {"count": pending_count}
```

**5.2 Add usePendingApprovalCount hook**

```tsx
export function usePendingApprovalCount() {
  return useQuery({
    queryKey: ["approvals", "pending", "count"],
    queryFn: async () => {
      const response = await api.get("/api/approvals/count/pending");
      return response.data.count;
    },
    refetchInterval: 30000, // Poll every 30s
  });
}
```

**5.3 Update sidebar with notification badge**

```tsx
// In sidebar navigation

const { data: pendingCount } = usePendingApprovalCount();

<NavItem href="/approvals" icon={Bell}>
  Approvals
  {pendingCount > 0 && (
    <span className="ml-auto px-1.5 py-0.5 text-xs bg-sc-red text-white rounded-full">
      {pendingCount}
    </span>
  )}
</NavItem>;
```

**5.4 WebSocket subscription for real-time updates**

```tsx
// Subscribe to approval events
useEffect(() => {
  const unsubscribe = subscribeToEvent("approval_request", (data) => {
    // Show toast notification
    toast({
      title: "Approval Required",
      description: data.title,
      action: <Button onClick={() => navigate(`/agents/${data.agent_id}`)}>View</Button>,
    });
    // Invalidate count
    queryClient.invalidateQueries({ queryKey: ["approvals", "pending", "count"] });
  });

  return unsubscribe;
}, []);
```

### Phase 6: Edge Cases

**6.1 Timeout handling** (already in \_wait_for_approval)

- Auto-deny after 5 minutes
- Update status to `expired`
- Agent continues with denial

**6.2 Agent terminated while waiting**

- `cancel_all()` method exists
- Call from termination handlers
- Wake up waiters with denial

**6.3 Approval status sync**

- When loading approval in UI, check if still pending
- If agent no longer waiting, update status

## Message Flow Diagram

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Agent     │     │   Worker    │     │    API      │     │     UI      │
│  (Claude)   │     │  Process    │     │   Process   │     │  (Browser)  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │                   │
       │ Tool: rm -rf      │                   │                   │
       │──────────────────>│                   │                   │
       │                   │                   │                   │
       │                   │ Create Approval   │                   │
       │                   │──────────────────>│ (Graph)           │
       │                   │                   │                   │
       │                   │ Subscribe Redis   │                   │
       │                   │ approval:{id}     │                   │
       │                   │                   │                   │
       │                   │ Broadcast WS      │                   │
       │                   │───────────────────────────────────────>│
       │                   │                   │                   │
       │                   │ Block on Event    │                   │ Show in thread
       │                   │ (max 5 min)       │                   │
       │                   │                   │                   │
       │                   │                   │                   │ User clicks
       │                   │                   │                   │ "Approve"
       │                   │                   │<──────────────────│
       │                   │                   │                   │
       │                   │                   │ Update Graph      │
       │                   │                   │                   │
       │                   │ Publish Redis     │                   │
       │                   │<──────────────────│ approval:{id}     │
       │                   │                   │                   │
       │                   │ Event.set()       │                   │
       │                   │                   │                   │
       │ permissionDecision│                   │                   │
       │<──────────────────│ = 'allow'         │                   │
       │                   │                   │                   │
       │ Execute tool      │                   │                   │
       │                   │                   │                   │
```

## Files to Modify

### Backend

1. `apps/api/src/sibyl/agents/redis_sub.py` - **NEW**: Direct Redis subscription helper for worker
2. `apps/api/src/sibyl/agents/approvals.py` - Use redis_sub for waiting, add status updates
3. `apps/api/src/sibyl/api/routes/approvals.py` - Publish to worker channel + WebSocket, add count
   endpoint
4. `apps/api/src/sibyl/jobs/worker.py` - Store approval_request messages as AgentMessages

### Frontend

1. `apps/web/src/components/agents/approval-request-message.tsx` - NEW
2. `apps/web/src/components/agents/agent-chat-panel.tsx` - Render approval messages
3. `apps/web/src/lib/hooks.ts` - Add approval hooks
4. `apps/web/src/components/layout/sidebar.tsx` - Add notification badge
5. `apps/web/src/components/agents/approval-queue.tsx` - Update if needed

### Models

1. `AgentStatus.WAITING_APPROVAL` — ✅ Already exists (line 25, agents.py)
2. Add `AgentMessageType.APPROVAL_REQUEST` if not exists

## Testing Plan

1. **Unit tests**: ApprovalService pubsub subscription
2. **Integration tests**: Full approval flow with Redis
3. **E2E tests**: UI approval in thread
4. **Timeout tests**: Verify 5-minute expiry works
5. **Concurrent tests**: Multiple approvals, multiple agents

## Rollout Plan

1. Deploy backend changes (backward compatible)
2. Test with internal agents
3. Deploy frontend changes
4. Monitor approval latency and success rates
