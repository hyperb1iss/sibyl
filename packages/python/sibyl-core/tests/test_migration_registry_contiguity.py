"""Migration registries have no version gaps.

`apply_schema_migrations` applies every registered version above the recorded
one, so a registry that skips a number would leave any namespace brought past
the gap without that migration forever. Parallel branches take their numbers
from this registry; a gap here means a branch merged out of order.
"""

from __future__ import annotations

from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    _content_schema_migrations,
)
from sibyl_core.backends.surreal.schema import GRAPH_SCHEMA_MIGRATIONS
from sibyl_core.backends.surreal.schema_version import GRAPH_SCHEMA_CURRENT_VERSION


def test_content_migration_versions_are_contiguous() -> None:
    versions = [migration.version for migration in _content_schema_migrations(url="")]
    assert versions == list(range(versions[0], CONTENT_SCHEMA_CURRENT_VERSION + 1))


def test_graph_migration_versions_are_contiguous() -> None:
    versions = [migration.version for migration in GRAPH_SCHEMA_MIGRATIONS]
    assert versions == list(range(versions[0], GRAPH_SCHEMA_CURRENT_VERSION + 1))
