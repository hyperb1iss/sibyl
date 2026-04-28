"""Helpers for logging sensitive text by fingerprint."""

from __future__ import annotations

import hashlib


def fingerprint_text(value: str | None) -> str:
    normalized = " ".join((value or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def text_log_fields(value: str | None, *, field: str) -> dict[str, int | str]:
    text = value or ""
    normalized = " ".join(text.split())
    return {
        f"{field}_hash": fingerprint_text(text),
        f"{field}_length": len(text),
        f"{field}_word_count": len(normalized.split()) if normalized else 0,
    }


def query_log_fields(query: str | None) -> dict[str, int | str]:
    return text_log_fields(query, field="query")


__all__ = ["fingerprint_text", "query_log_fields", "text_log_fields"]
