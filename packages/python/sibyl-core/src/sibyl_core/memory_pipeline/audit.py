"""Lossless storage of conditional procedure audit artifacts.

Surreal flexible objects omit null members. Store the audit as one canonical
JSON value, while keeping model-facing metadata and legacy object rows intact.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

_KEY = "conditional_procedure"
AUDIT_STORAGE_PREFIX = "sibyl-audit-json-v1:"


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def decode_audit_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    result = dict(metadata)
    value = result.get(_KEY)
    if isinstance(value, str) and value.startswith(AUDIT_STORAGE_PREFIX):
        encoded = value[len(AUDIT_STORAGE_PREFIX) :]
        try:
            decoded = json.loads(encoded)
            if not isinstance(decoded, dict) or _canonical(decoded) != encoded:
                raise ValueError("noncanonical audit object")
        except (ValueError, TypeError, RecursionError):
            # Metadata is also user-authored. Preserve malformed values for
            # artifact validation without making unrelated memory unreadable.
            return result
        result[_KEY] = decoded
    return result


def encode_audit_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    result = decode_audit_metadata(metadata)
    if isinstance(result.get(_KEY), dict):
        result[_KEY] = AUDIT_STORAGE_PREFIX + _canonical(result[_KEY])
    return result
