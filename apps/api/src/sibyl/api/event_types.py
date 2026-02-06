"""WebSocket event type constants.

Centralizes event names to prevent typos causing silent failures.
Frontend types in apps/web/src/lib/websocket.ts must stay in sync.
"""

from enum import StrEnum


class WSEvent(StrEnum):
    """WebSocket event types published by the backend."""

    ENTITY_CREATED = "entity_created"
    ENTITY_UPDATED = "entity_updated"
    ENTITY_DELETED = "entity_deleted"
    SEARCH_COMPLETE = "search_complete"
    CRAWL_STARTED = "crawl_started"
    CRAWL_PROGRESS = "crawl_progress"
    CRAWL_COMPLETE = "crawl_complete"
    HEALTH_UPDATE = "health_update"
    AGENT_STATUS = "agent_status"
    AGENT_MESSAGE = "agent_message"
    AGENT_WORKSPACE = "agent_workspace"
    APPROVAL_RESPONSE = "approval_response"
    PERMISSION_CHANGED = "permission_changed"
    NOTE_CREATED = "note_created"
    NOTE_PENDING = "note_pending"
    INTER_AGENT_MESSAGE = "inter_agent_message"
    STATUS_HINT = "status_hint"
    BACKUP_STARTED = "backup_started"
    BACKUP_COMPLETE = "backup_complete"
    BACKUP_FAILED = "backup_failed"
    CRAWL_SYNC_COMPLETE = "crawl_sync_complete"
    GRAPH_UPDATED = "graph_updated"
    ENTITY_PENDING = "entity_pending"
    QUESTION_ANSWERED = "question_answered"
    AGENT_UPDATE = "agent_update"
