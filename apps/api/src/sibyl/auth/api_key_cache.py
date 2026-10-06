"""Remember which presented API keys already passed the KDF check."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID


def api_key_digest(raw_key: str) -> str:
    """Cache key for a presented credential; the raw key itself is never kept."""
    return sha256(raw_key.encode("utf-8")).hexdigest()


def api_key_hash_fingerprint(*, salt_hex: str, hash_hex: str) -> str:
    """Identify the stored hash a verdict was reached against."""
    return sha256(f"{salt_hex}\n{hash_hex}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedApiKey:
    api_key_id: str
    hash_fingerprint: str
    verified_at: float


class VerifiedApiKeyCache:
    """Process-local memo of ``verify_api_key`` verdicts.

    Only the fact "this presented key matched api_keys row X while X stored
    hash Y" is remembered. Every authentication still reads the row, so
    revocation, expiry and scope changes keep their exact semantics; a row
    whose stored hash changed misses on the fingerprint and is verified again.
    The LRU bound and TTL are backstops, not the invalidation mechanism.
    """

    def __init__(self, *, max_entries: int = 4096, ttl_seconds: float = 600.0) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, VerifiedApiKey] = OrderedDict()

    def get(self, digest: str, *, now: float | None = None) -> VerifiedApiKey | None:
        current = time.monotonic() if now is None else now
        entry = self._entries.get(digest)
        if entry is None:
            return None
        if current - entry.verified_at >= self._ttl_seconds:
            self._entries.pop(digest, None)
            return None
        self._entries.move_to_end(digest)
        return entry

    def store(
        self,
        digest: str,
        *,
        api_key_id: UUID | str,
        hash_fingerprint: str,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else now
        self._entries[digest] = VerifiedApiKey(
            api_key_id=str(api_key_id),
            hash_fingerprint=hash_fingerprint,
            verified_at=current,
        )
        self._entries.move_to_end(digest)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def invalidate_key(self, api_key_id: UUID | str) -> None:
        wanted = str(api_key_id)
        for digest, entry in list(self._entries.items()):
            if entry.api_key_id == wanted:
                self._entries.pop(digest, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


verified_api_key_cache = VerifiedApiKeyCache()
