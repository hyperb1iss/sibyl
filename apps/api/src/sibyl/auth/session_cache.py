"""Short-circuit access-session checks by sid."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sibyl_core.auth import AuthSession


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class AccessSessionState:
    session_id: UUID
    user_id: UUID | None
    organization_id: UUID | None
    revoked: bool
    expires_at: datetime


class AccessSessionCache:
    """Process-local verdicts on whether an access session is still valid.

    Every change that can make a cached "valid" wrong (a revocation here, or
    an invalidation announced by another replica) moves ``generation`` on. A
    fill reads the generation before its database read and hands it back to
    ``store_session``, which drops the fill if the generation moved meanwhile:
    a read that raced a revocation must not re-cache the session as valid.
    """

    def __init__(self, *, max_entries: int = 10_000) -> None:
        self._max_entries = max_entries
        self._entries: dict[UUID, AccessSessionState] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def get(self, session_id: UUID, *, now: datetime | None = None) -> bool | None:
        current = _naive_utc(now) if now is not None else _utcnow()
        state = self._entries.get(session_id)
        if state is None:
            return None
        if state.expires_at <= current:
            self._entries.pop(session_id, None)
            return None
        return not state.revoked

    def store_session(
        self,
        session: AuthSession,
        *,
        now: datetime | None = None,
        generation: int | None = None,
    ) -> None:
        if generation is not None and generation != self._generation:
            return
        current = _naive_utc(now) if now is not None else _utcnow()
        expires_at = _naive_utc(session.refresh_token_expires_at or session.expires_at)
        if expires_at <= current:
            self._entries.pop(session.id, None)
            return
        self._set(
            AccessSessionState(
                session_id=session.id,
                user_id=session.user_id,
                organization_id=session.organization_id,
                revoked=session.revoked_at is not None,
                expires_at=expires_at,
            )
        )

    def mark_revoked(
        self,
        session_id: UUID,
        *,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        self._generation += 1
        current = _naive_utc(now) if now is not None else _utcnow()
        state = self._entries.get(session_id)
        expiry = expires_at or (
            state.expires_at if state is not None else current + timedelta(minutes=5)
        )
        expiry = _naive_utc(expiry)
        if expiry <= current:
            expiry = current + timedelta(minutes=5)
        self._set(
            AccessSessionState(
                session_id=session_id,
                user_id=user_id or (state.user_id if state is not None else None),
                organization_id=organization_id
                or (state.organization_id if state is not None else None),
                revoked=True,
                expires_at=expiry,
            )
        )

    def invalidate(self, session_id: UUID) -> None:
        self._generation += 1
        self._entries.pop(session_id, None)

    def invalidate_user(self, user_id: UUID) -> None:
        self._generation += 1
        for session_id, state in list(self._entries.items()):
            if state.user_id == user_id:
                self._entries.pop(session_id, None)

    def invalidate_organization(self, organization_id: UUID) -> None:
        self._generation += 1
        for session_id, state in list(self._entries.items()):
            if state.organization_id == organization_id:
                self._entries.pop(session_id, None)

    def clear(self) -> None:
        self._generation += 1
        self._entries.clear()

    def _set(self, state: AccessSessionState) -> None:
        self._entries[state.session_id] = state
        while len(self._entries) > self._max_entries:
            self._entries.pop(next(iter(self._entries)))


access_session_cache = AccessSessionCache()
