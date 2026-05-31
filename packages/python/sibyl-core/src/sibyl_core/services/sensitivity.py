"""Deterministic sensitivity classification for source records."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sibyl_core.models.sources import SourcePrivacyClass, SourceRecord

_SSN_RE = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_TWO_FACTOR_RE = re.compile(
    r"\b(?:2fa|two[-_ ]factor|one[-_ ]time|otp)"
    r"(?:[-_ ](?:code|passcode|token|password))?\s*(?:is|was|[:=])\s*\d{4,8}\b"
    r"|\bverification[-_ ](?:code|passcode|token)\s*(?:is|was|[:=])\s*\d{4,8}\b",
    re.IGNORECASE,
)
_HIGH_ENTROPY_RE = re.compile(
    r"\b(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token|token|password|passwd)"
    r"\b\s*(?:is|was|[:=])?\s*['\"]?([A-Za-z0-9_./+=-]{32,256})\b",
    re.IGNORECASE,
)
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "api_key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "api_key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"), "api_key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "api_key"),
    (
        re.compile(
            r"\b(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token|password|passwd)"
            r"\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}",
            re.IGNORECASE,
        ),
        "api_key",
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE), "api_key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private_key"),
)
_PII_FLAGS = frozenset({"payment_card", "ssn"})
_SECRET_FLAGS = frozenset({"api_key", "high_entropy_token", "private_key", "two_factor_code"})
_TEXT_LIMIT = 80_000
_ENTROPY_THRESHOLD = 4.0


@dataclass(frozen=True, slots=True)
class SensitivityClassification:
    contains_pii: bool
    contains_secret: bool
    sensitivity_flags: tuple[str, ...]
    classifier: str = "deterministic-v1"

    @property
    def contains_sensitive(self) -> bool:
        return self.contains_pii or self.contains_secret

    @property
    def privacy_class(self) -> SourcePrivacyClass | None:
        if self.contains_secret:
            return SourcePrivacyClass.SENSITIVE
        return None

    def metadata(self) -> dict[str, object]:
        return {
            "contains_pii": self.contains_pii,
            "contains_secret": self.contains_secret,
            "contains_sensitive": self.contains_sensitive,
            "sensitivity_classifier": self.classifier,
            "sensitivity_flags": list(self.sensitivity_flags),
            "sensitivity_state": "classified",
        }


def classify_record(record: SourceRecord) -> SensitivityClassification:
    flags: list[str] = []
    text = _classification_text(record)

    if _SSN_RE.search(text):
        _append_flag(flags, "ssn")
    if any(_luhn_valid(candidate) for candidate in _CARD_RE.findall(text)):
        _append_flag(flags, "payment_card")
    for pattern, flag in _SECRET_PATTERNS:
        if pattern.search(text):
            _append_flag(flags, flag)
    if _TWO_FACTOR_RE.search(text):
        _append_flag(flags, "two_factor_code")
    if any(_is_high_entropy_secret(match.group(1)) for match in _HIGH_ENTROPY_RE.finditer(text)):
        _append_flag(flags, "high_entropy_token")

    flag_set = frozenset(flags)
    return SensitivityClassification(
        contains_pii=bool(flag_set & _PII_FLAGS),
        contains_secret=bool(flag_set & _SECRET_FLAGS),
        sensitivity_flags=tuple(flags),
    )


def _classification_text(record: SourceRecord) -> str:
    parts: list[str] = [
        record.title,
        record.body,
        *record.participants,
        *record.labels,
        *_metadata_strings(record.metadata),
    ]
    return "\n".join(part for part in parts if part)[:_TEXT_LIMIT]


def _metadata_strings(value: object, *, key: str | None = None) -> Iterable[str]:
    if isinstance(value, str):
        yield f"{key}: {value}" if key else value
    elif isinstance(value, Mapping):
        for item_key, item in value.items():
            item_key_text = str(item_key)
            path = f"{key}.{item_key_text}" if key else item_key_text
            yield from _metadata_strings(item, key=path)
    elif isinstance(value, Iterable) and not isinstance(value, bytes | bytearray):
        for item in value:
            yield from _metadata_strings(item, key=key)
    elif value is not None:
        text = str(value)
        yield f"{key}: {text}" if key else text


def _append_flag(flags: list[str], flag: str) -> None:
    if flag not in flags:
        flags.append(flag)


def _luhn_valid(candidate: str) -> bool:
    digits = [int(char) for char in candidate if char.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False

    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _is_high_entropy_secret(candidate: str) -> bool:
    if len(candidate) < 32 or candidate.isdigit():
        return False
    if candidate.islower() or candidate.isupper():
        return False
    if len({char.isdigit() for char in candidate}) < 2:
        return False
    return _shannon_entropy(candidate) >= _ENTROPY_THRESHOLD


def _shannon_entropy(value: str) -> float:
    length = len(value)
    frequencies = {char: value.count(char) for char in set(value)}
    return -sum((count / length) * math.log2(count / length) for count in frequencies.values())


__all__ = ["SensitivityClassification", "classify_record"]
