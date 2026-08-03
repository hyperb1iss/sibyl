"""Writer-declared retrieval keys: the exact-match contract for a memory.

A retrieval key is an identifier the writing agent asserts this memory answers
to: an error string, a symbol name, a config flag, an alias. It is not derived
from the content, so a key may name something the body never spells out, and
that is the whole point -- dense similarity and full-text both need the token to
appear somewhere in the text before they can find it.

Matching semantics, which every surface has to agree on:

* A key is compared by Unicode casefold, so ``ERR_CONN_RESET`` and
  ``err_conn_reset`` are the same key. The verbatim form is kept for display.
* Internal whitespace runs collapse to one space, so a key copied out of
  wrapped text still matches one typed on a single line.
* Nothing else is transformed. No stemming, no punctuation stripping, no
  tokenization: an exact key is exact, which is what buys the precision.

Two entry points, deliberately asymmetric. ``normalize_retrieval_keys`` refuses
a violation, because a write boundary is where a caller can still be told its
key list is too long. ``coerce_retrieval_keys`` drops what it cannot use,
because the storage edge reads rows written by older code and must not fail a
whole projection over one bad key.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

# A key names a memory rather than describing it, so it is bounded by the same
# length as a title (sibyl_core.tools.helpers.MAX_TITLE_LENGTH, pinned equal by
# test_retrieval_keys.py). Anything longer is prose and belongs in the content.
MAX_RETRIEVAL_KEY_LENGTH = 200
# A writer declaring more than this many exact keys is describing a document
# rather than naming it, and the cap also bounds the indexed payload a single
# row can add (16 * 200 chars).
MAX_RETRIEVAL_KEYS = 16

_WHITESPACE_RUN = re.compile(r"\s+")

__all__ = [
    "MAX_RETRIEVAL_KEYS",
    "MAX_RETRIEVAL_KEY_LENGTH",
    "coerce_retrieval_keys",
    "normalize_retrieval_keys",
    "retrieval_key_match_form",
]


def retrieval_key_match_form(value: str) -> str:
    """The form two keys are compared in: casefolded, whitespace-collapsed."""

    return _WHITESPACE_RUN.sub(" ", value).strip().casefold()


def _display_form(value: str) -> str:
    return _WHITESPACE_RUN.sub(" ", value).strip()


def _printable(value: str) -> bool:
    # A control character in a key cannot have been typed on purpose and would
    # not survive a round trip through a query string.
    return all(unicodedata.category(char) != "Cc" for char in value)


def normalize_retrieval_keys(values: Iterable[str] | None) -> tuple[list[str], list[str]]:
    """Validate a writer's declared keys into (display forms, match forms).

    Both lists are the same length and in the same order: index ``i`` of the
    match list is the comparable form of index ``i`` of the display list. The
    display list keeps the writer's casing, deduplicated by match form so
    ``ERR_X`` and ``err_x`` collapse to whichever was declared first.

    Raises ``ValueError`` when a key is longer than
    ``MAX_RETRIEVAL_KEY_LENGTH`` or when more than ``MAX_RETRIEVAL_KEYS``
    distinct keys are declared. Truncating instead would silently ship a key
    the writer did not declare, which fails at retrieval time with no receipt.
    """

    if values is None:
        return [], []

    display: list[str] = []
    match: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"retrieval key must be a string, got {type(raw).__name__}")
        shown = _display_form(raw)
        if not shown:
            continue
        if len(shown) > MAX_RETRIEVAL_KEY_LENGTH:
            raise ValueError(
                f"retrieval key exceeds {MAX_RETRIEVAL_KEY_LENGTH} characters: {shown[:60]!r}..."
            )
        if not _printable(shown):
            raise ValueError(f"retrieval key contains control characters: {shown[:60]!r}")
        comparable = retrieval_key_match_form(shown)
        if comparable in seen:
            continue
        seen.add(comparable)
        display.append(shown)
        match.append(comparable)

    if len(display) > MAX_RETRIEVAL_KEYS:
        raise ValueError(
            f"at most {MAX_RETRIEVAL_KEYS} retrieval keys may be declared, got {len(display)}"
        )
    return display, match


def coerce_retrieval_keys(value: object) -> tuple[list[str], list[str]] | None:
    """Best-effort read of keys off a metadata bag or a stored row.

    Returns ``None`` when the value says nothing about keys, which the write
    path treats as "this write does not speak to retrieval keys" and therefore
    preserves whatever the row already carries. An empty list is a statement
    (no keys) and comes back as empty lists.
    """

    if value is None:
        return None
    if isinstance(value, str):
        candidates: Sequence[object] = [value]
    elif isinstance(value, Sequence):
        candidates = value
    else:
        return None

    display: list[str] = []
    match: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        shown = _display_form(raw)[:MAX_RETRIEVAL_KEY_LENGTH]
        if not shown or not _printable(shown):
            continue
        comparable = retrieval_key_match_form(shown)
        if comparable in seen:
            continue
        seen.add(comparable)
        display.append(shown)
        match.append(comparable)
        if len(display) == MAX_RETRIEVAL_KEYS:
            break
    return display, match
