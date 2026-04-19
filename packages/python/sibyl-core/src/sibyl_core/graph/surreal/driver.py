"""Compatibility shim for the legacy `sibyl_core.graph.surreal.driver` path."""

from sibyl_core.backends.surreal.driver import (
    SurrealDriver,
    SurrealDriverSession,
    _namespace_for_group,
)

__all__ = ["SurrealDriver", "SurrealDriverSession", "_namespace_for_group"]

