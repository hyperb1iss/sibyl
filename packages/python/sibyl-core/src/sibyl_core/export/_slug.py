"""Filesystem-safe slugs shared by the export projections."""

from __future__ import annotations

import re

_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_slug(value: str, *, fallback: str = "concept") -> str:
    """Reduce arbitrary text to a lowercase filename-safe slug."""
    slug = _UNSAFE.sub("-", value.strip()).strip(".-").lower()
    return slug or fallback


__all__ = ["safe_slug"]
