"""Deciding when a query is asking for an identifier rather than a topic.

The exact-match arm is only worth a read when the query actually carries
something that could be an exact key. "how do we handle authentication" has no
such token and must leave the arm inert; "why does ERR_CONN_RESET_0x7f31 fire"
has exactly one and should probe it.

The test is mechanical and per-token: no scoring, no thresholds, no model. A
token is identifier-shaped when its own characters say so, because identifier
syntax is visibly different from English orthography. The rules, each a named
predicate below so the matrix test can target it:

* a quoted span, which is the caller explicitly asking for an exact string
  (double quotes or backticks only; a bare apostrophe is English possessive
  far more often than it is a quote)
* letters and digits mixed in one token (``0x7f31``, ``sha256``, ``v2beta``),
  excluding ordinals and clock times (``3rd``, ``21st``, ``10am``,
  ``10am-11am``), which mix the two character classes while being plainly prose
* an underscore (``ERR_CONN_RESET``, ``snake_case``)
* a ``::`` path separator (``std::vector``)
* dotted segments of two or more characters each (``search.py``,
  ``sibyl_core.retrieval``), which admits file names and module paths while
  rejecting ``e.g``, ``i.e`` and a sentence's trailing period
* a hexadecimal run of eight or more characters containing at least one digit
  (a commit SHA, a pointer), the digit being what separates a SHA from an
  English word that happens to spell in ``a-f``
* a lower-to-upper transition inside one alphanumeric token (``EntityManager``,
  ``getUserById``), which a capitalized sentence opener does not have
* a long command-line option (``--wait-searchable``), which carries none of the
  other shapes and which English never produces

One shape is deliberately absent: a bare all-caps acronym such as ``TTL`` or
``EFC``. English writes acronyms exactly the same way, so admitting them would
fire the arm on ordinary emphasis. A writer who needs one found declares it as
a key and a caller quotes it.

False positives are cheap and false negatives are not. A token that looks like
an identifier but matches no declared key costs one indexed read and returns
nothing, while a missed identifier costs the hit the whole arm exists to find.
"""

from __future__ import annotations

import re
from itertools import pairwise

from sibyl_core.memory_pipeline.retrieval_keys import (
    MAX_RETRIEVAL_KEYS,
    retrieval_key_match_form,
)

# A probe cannot usefully carry more distinct keys than one memory is allowed to
# declare, so the writer's cap bounds the reader's too.
MAX_IDENTIFIER_PROBE_TOKENS = MAX_RETRIEVAL_KEYS
# Below three characters there is no room for a shape: every rule needs at least
# a separator or a case transition plus something on both sides of it.
MIN_IDENTIFIER_TOKEN_LENGTH = 3

_QUOTED_SPAN = re.compile(r"\"([^\"]+)\"|`([^`]+)`")
# Angle brackets stay: they are generic-type syntax far more often than prose
# punctuation, and stripping them would cut `std::vector<int>` down to a key
# nobody declared.
_LEADING_PUNCTUATION = "([{\"'"
_TRAILING_PUNCTUATION = ")]},;:!?.\"'"
_HEX_RUN = re.compile(r"[0-9a-fA-F]{8,}")
_ENGLISH_NUMBER_WORD = re.compile(r"\d+(?:(?:st|nd|rd|th)s?|am|pm)", re.IGNORECASE)
_WORD_CHARACTER = re.compile(r"[^\W_]", re.UNICODE)

__all__ = [
    "MAX_IDENTIFIER_PROBE_TOKENS",
    "MIN_IDENTIFIER_TOKEN_LENGTH",
    "identifier_probe_tokens",
    "is_identifier_shaped_query",
    "is_identifier_shaped_token",
]


def _is_english_number_word(token: str) -> bool:
    """Digits carrying an English suffix: an ordinal or a clock time.

    These mix letters and digits, so the shape rule would admit them, and they
    are plainly prose. Tested per hyphen-separated segment, because a token is
    still prose when the number word is only part of it (`10am-11am`), and a
    trailing plural is allowed so `3rds` reads the same as `3rd`.

    A bare `<digits>s` is deliberately NOT here. A decade (`1990s`) and a
    duration (`30s`, `300s`, `443s`) are indistinguishable by their characters,
    durations appear verbatim in the error strings this arm exists to match, and
    the costs are asymmetric: a false positive is one indexed read returning
    nothing, a false negative loses the hit. So the duration wins and the decade
    fires.
    """

    segments = [segment for segment in token.split("-") if segment]
    if not segments:
        return False
    digit_segments = [segment for segment in segments if any(c.isdigit() for c in segment)]
    if not digit_segments:
        return False
    return all(_ENGLISH_NUMBER_WORD.fullmatch(segment) for segment in digit_segments)


def _has_letter_and_digit(token: str) -> bool:
    if _is_english_number_word(token):
        return False
    return any(char.isalpha() for char in token) and any(char.isdigit() for char in token)


def _has_underscore(token: str) -> bool:
    # Leading and trailing underscores are decoration; the rule is about an
    # underscore joining two parts of a name.
    stripped = token.strip("_")
    return "_" in stripped and bool(_WORD_CHARACTER.search(stripped))


def _has_scope_separator(token: str) -> bool:
    return "::" in token


def _has_dotted_segments(token: str) -> bool:
    if "." not in token:
        return False
    segments = token.split(".")
    if len(segments) < 2:
        return False
    return all(len(segment) >= 2 and _WORD_CHARACTER.search(segment) for segment in segments)


def _has_hex_run(token: str) -> bool:
    return any(any(char.isdigit() for char in run.group()) for run in _HEX_RUN.finditer(token))


def _has_case_transition(token: str) -> bool:
    return any(previous.islower() and current.isupper() for previous, current in pairwise(token))


def _is_command_flag(token: str) -> bool:
    # A long option is an identifier that carries none of the other shapes:
    # --wait-searchable has no digit, underscore, dot or case transition. English
    # never opens a word with a double hyphen, so the prefix is unambiguous.
    return token.startswith("--") and bool(_WORD_CHARACTER.search(token[2:3]))


_TOKEN_SHAPE_RULES = (
    _has_letter_and_digit,
    _has_underscore,
    _has_scope_separator,
    _has_dotted_segments,
    _has_hex_run,
    _has_case_transition,
    _is_command_flag,
)


def is_identifier_shaped_token(token: str) -> bool:
    """Whether one bare token (already unquoted, already trimmed) is a probe."""

    if len(token) < MIN_IDENTIFIER_TOKEN_LENGTH:
        return False
    if not _WORD_CHARACTER.search(token):
        return False
    return any(rule(token) for rule in _TOKEN_SHAPE_RULES)


def _trim_token(token: str) -> str:
    return token.lstrip(_LEADING_PUNCTUATION).rstrip(_TRAILING_PUNCTUATION)


def identifier_probe_tokens(query: str) -> tuple[str, ...]:
    """The exact-key probes a query carries, in match form, order preserved.

    An empty result means the exact-match arm does not fire at all: no read is
    issued and the fused pool is byte-identical to what the query would have
    produced before keys existed.
    """

    if not query or not query.strip():
        return ()

    probes: list[str] = []
    seen: set[str] = set()

    def offer(value: str) -> None:
        comparable = retrieval_key_match_form(value)
        if not comparable or comparable in seen:
            return
        seen.add(comparable)
        probes.append(comparable)

    residual = query
    for span in _QUOTED_SPAN.finditer(query):
        quoted = span.group(1) or span.group(2) or ""
        # A quoted span is taken whole, spaces and all: an error string is the
        # canonical multi-word exact key, and the quotes are the caller saying
        # so. Only the length floor applies.
        if len(quoted.strip()) >= MIN_IDENTIFIER_TOKEN_LENGTH:
            offer(quoted)
        residual = residual.replace(span.group(0), " ", 1)

    for raw in residual.split():
        token = _trim_token(raw)
        if is_identifier_shaped_token(token):
            offer(token)

    return tuple(probes[:MAX_IDENTIFIER_PROBE_TOKENS])


def is_identifier_shaped_query(query: str) -> bool:
    return bool(identifier_probe_tokens(query))
