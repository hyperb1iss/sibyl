"""Member handles: the name an organization member's sibylnet inbox goes by.

Every member holds one handle, unique in the organization and spelled in the
channel charset, so ``@<handle>`` is that member's inbox and nothing else's.
A handle is assigned once, from the member's email local part, lowercased,
with anything outside the charset turned into ``-``; a taken or reserved one
gets ``-2``, ``-3`` and so on, in the order members joined. Handles do not
change afterwards, so an inbox and its threads stay with one person.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Collection, Iterable

MAX_HANDLE_CHARS = 64
FALLBACK_HANDLE = "member"
# ``@me`` names the caller's own inbox wherever a channel is accepted, and the
# others read as a broadcast to whoever sees them; no member holds any of them.
SELF_HANDLE = "me"
RESERVED_HANDLES = frozenset({SELF_HANDLE, "all", "everyone", "here"})
_OUTSIDE_CHARSET = re.compile(r"[^a-z0-9._-]+")
_LEADING_PUNCTUATION = re.compile(r"^[._-]+")


def handle_base(email: str | None, name: str | None = None) -> str:
    """The handle a member starts from: their email local part, else their name."""
    for source in (email.partition("@")[0] if email else "", name or ""):
        candidate = _OUTSIDE_CHARSET.sub("-", source.strip().lower())
        candidate = _LEADING_PUNCTUATION.sub("", candidate)[:MAX_HANDLE_CHARS].rstrip("-")
        if candidate:
            return candidate
    return FALLBACK_HANDLE


def pick_handle(base: str, taken: Collection[str]) -> str:
    """``base`` if it is free and not reserved, else the first free ``base-2``, ``base-3``, ..."""
    if base not in taken and base not in RESERVED_HANDLES:
        return base
    number = 2
    while True:
        suffix = f"-{number}"
        candidate = base[: MAX_HANDLE_CHARS - len(suffix)].rstrip("-") + suffix
        if candidate not in taken:
            return candidate
        number += 1


def closest_handles(wanted: str, handles: Iterable[str], limit: int = 3) -> list[str]:
    """Up to ``limit`` handles spelled most like ``wanted``, best first."""
    return difflib.get_close_matches(wanted, list(handles), n=limit, cutoff=0.5)


__all__ = [
    "FALLBACK_HANDLE",
    "MAX_HANDLE_CHARS",
    "RESERVED_HANDLES",
    "SELF_HANDLE",
    "closest_handles",
    "handle_base",
    "pick_handle",
]
