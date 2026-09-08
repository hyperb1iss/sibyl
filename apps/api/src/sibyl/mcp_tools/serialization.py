"""Serialization helpers for MCP tool responses."""

from dataclasses import asdict, fields, is_dataclass
from typing import Any

from sibyl_core.memory_pipeline.source_lifecycle import public_memory_metadata
from sibyl_core.tools.responses import EntitySummary, NeighborEntity, SearchResult


def _filter_source_clocks(obj: Any, payload: Any) -> None:
    """Filter typed response rows, leaving arbitrary dictionaries opaque."""
    if is_dataclass(obj) and not isinstance(obj, type):
        for item in fields(obj):
            value = getattr(obj, item.name)
            if item.name == "metadata" and isinstance(
                obj, EntitySummary | NeighborEntity | SearchResult
            ):
                payload[item.name] = public_memory_metadata(payload[item.name])
            else:
                _filter_source_clocks(value, payload[item.name])
    elif isinstance(obj, list | tuple):
        for value, serialized in zip(obj, payload, strict=True):
            _filter_source_clocks(value, serialized)


def to_dict(obj: Any) -> Any:
    """Serialize dataclasses, omitting live source clocks from row metadata."""
    if is_dataclass(obj) and not isinstance(obj, type):
        payload = asdict(obj)
        _filter_source_clocks(obj, payload)
        return payload
    if isinstance(obj, list):
        return [to_dict(item) for item in obj]
    return obj
