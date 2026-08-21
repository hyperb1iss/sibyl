"""Serialization helpers for MCP tool responses."""

from dataclasses import asdict
from typing import Any


def to_dict(obj: Any) -> Any:
    """Convert dataclass values recursively for JSON serialization."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [to_dict(item) for item in obj]
    return obj
