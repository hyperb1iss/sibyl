"""Shared utilities for sandbox controller and dispatcher."""

from __future__ import annotations

from typing import Any


def set_if_present(model: Any, attr: str, value: Any) -> None:
    """Set attribute on model only if the attribute exists."""
    if hasattr(model, attr):
        setattr(model, attr, value)


def status_of(model: Any) -> str:
    """Extract normalized lowercase status string from a model instance."""
    value = getattr(model, "status", "") or ""
    return str(value).lower()
