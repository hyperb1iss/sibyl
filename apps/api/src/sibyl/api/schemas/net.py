"""sibylnet request and response models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from sibyl_core.services.net import (
    MAX_AGENT_CHARS,
    MAX_SESSION_CHARS,
    normalize_body,
    normalize_channel,
    normalize_label,
    normalize_thread,
)

NetMessageKindLabel = Literal["message", "decision"]


class NetMessageCreateRequest(BaseModel):
    """One message for a channel; ``kind=decision`` is also remembered."""

    channel: str = Field(description="`@name` for an agent inbox, `#name` for a shared room")
    body: str = Field(description="Message text, 1 to 16000 characters after stripping")
    thread: str | None = Field(
        default=None,
        description="Free-text thread, e.g. `gh:acme/app#42`, a task id or a slug",
    )
    kind: NetMessageKindLabel = "message"
    project_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Graph project the thread belongs to. The poster must be a contributor, and the "
            "first post that names one pins the thread to it"
        ),
    )
    agent: str | None = Field(
        default=None,
        max_length=MAX_AGENT_CHARS,
        description="Client-asserted agent label such as `claude-code`; a label, not identity",
    )
    session: str | None = Field(
        default=None,
        max_length=MAX_SESSION_CHARS,
        description="Client-asserted session id",
    )

    @field_validator("channel")
    @classmethod
    def _channel(cls, value: str) -> str:
        return normalize_channel(value)

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        return normalize_body(value)

    @field_validator("thread")
    @classmethod
    def _thread(cls, value: str | None) -> str | None:
        return normalize_thread(value) or None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str | None) -> str | None:
        return normalize_label(value, field="agent", max_chars=MAX_AGENT_CHARS)

    @field_validator("session")
    @classmethod
    def _session(cls, value: str | None) -> str | None:
        return normalize_label(value, field="session", max_chars=MAX_SESSION_CHARS)

    @field_validator("project_id")
    @classmethod
    def _project(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None


class NetAuthor(BaseModel):
    """Who posted: the authenticated user, plus the labels their client sent."""

    user_id: str
    name: str | None = None
    email: str | None = None
    agent: str | None = None
    session: str | None = None


class NetMessage(BaseModel):
    """A stored message as readers see it.

    Absent values default to null, so a response saved for idempotent replay
    (the content store drops null fields) reads back as it was sent.
    """

    id: str
    channel: str
    thread: str | None = None
    kind: NetMessageKindLabel
    body: str
    project_id: str | None = None
    author: NetAuthor
    api_key_id: str | None = None
    decision_memory_id: str | None = None
    created_at: datetime
    cursor: str = Field(description="Feed cursor positioned at this message")


class NetMessageCreateResponse(BaseModel):
    message: NetMessage
    decision_memory_id: str | None = None


class NetFeedResponse(BaseModel):
    messages: list[NetMessage] = Field(description="Oldest first")
    next_since: str | None = Field(
        description="Cursor to pass as `since` on the next read",
    )
    more: bool = Field(description="Whether settled messages remain after this page")


class NetChannel(BaseModel):
    channel: str
    message_count: int
    thread_count: int
    last_message_at: datetime | None


class NetChannelsResponse(BaseModel):
    channels: list[NetChannel]


class NetThread(BaseModel):
    channel: str
    thread: str | None
    project_id: str | None
    message_count: int
    first_message_at: datetime | None
    last_message_at: datetime | None
    reflected_through: datetime | None
    reflection_source_id: str | None
    reflection_error: str | None
    reflection_skipped: list[str] = Field(
        default_factory=list,
        description="Authors the last distillation could not reflect, as `<user id>: <reason>`",
    )


class NetThreadsResponse(BaseModel):
    threads: list[NetThread]


__all__ = [
    "NetAuthor",
    "NetChannel",
    "NetChannelsResponse",
    "NetFeedResponse",
    "NetMessage",
    "NetMessageCreateRequest",
    "NetMessageCreateResponse",
    "NetMessageKindLabel",
    "NetThread",
    "NetThreadsResponse",
]
