"""Tests for metadata access utilities.

Covers:
- utils/metadata.py - Safe metadata extraction functions
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sibyl_core.utils.metadata import (
    extract_meta,
    filter_by_meta,
    get_metadata,
    has_meta,
    match_meta,
    safe_attr,
    safe_meta,
)


# =============================================================================
# Test Fixtures
# =============================================================================
@dataclass
class MockEntity:
    """Mock entity for testing metadata utilities."""

    name: str | None = None
    status: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class MinimalEntity:
    """Entity with no metadata attribute."""

    name: str = "test"


# =============================================================================
# get_metadata Tests
# =============================================================================
class TestGetMetadata:
    """Tests for get_metadata function."""

    def test_returns_metadata_dict(self) -> None:
        """Returns entity's metadata dict when present."""
        entity = MockEntity(metadata={"key": "value"})
        result = get_metadata(entity)
        assert result == {"key": "value"}

    def test_returns_empty_dict_when_none(self) -> None:
        """Returns empty dict when metadata is None."""
        entity = MockEntity(metadata=None)
        result = get_metadata(entity)
        assert result == {}

    def test_returns_empty_dict_when_missing(self) -> None:
        """Returns empty dict when entity has no metadata attr."""
        entity = MinimalEntity()
        result = get_metadata(entity)
        assert result == {}

    def test_returns_empty_dict_for_empty_metadata(self) -> None:
        """Returns empty dict when metadata is empty."""
        entity = MockEntity(metadata={})
        result = get_metadata(entity)
        assert result == {}


# =============================================================================
# safe_meta Tests
# =============================================================================
class TestSafeMeta:
    """Tests for safe_meta function."""

    def test_returns_value_when_present(self) -> None:
        """Returns metadata value when key exists."""
        entity = MockEntity(metadata={"status": "active"})
        result = safe_meta(entity, "status")
        assert result == "active"

    def test_returns_none_when_key_missing(self) -> None:
        """Returns None when key is not in metadata."""
        entity = MockEntity(metadata={"other": "value"})
        result = safe_meta(entity, "missing")
        assert result is None

    def test_returns_default_when_key_missing(self) -> None:
        """Returns default value when key is not in metadata."""
        entity = MockEntity(metadata={})
        result = safe_meta(entity, "status", "pending")
        assert result == "pending"

    def test_returns_default_when_metadata_none(self) -> None:
        """Returns default when metadata is None."""
        entity = MockEntity(metadata=None)
        result = safe_meta(entity, "key", "default")
        assert result == "default"

    def test_returns_default_when_no_metadata_attr(self) -> None:
        """Returns default when entity has no metadata attribute."""
        entity = MinimalEntity()
        result = safe_meta(entity, "key", "fallback")
        assert result == "fallback"

    def test_returns_falsy_values_correctly(self) -> None:
        """Returns falsy values (0, '', False) when they exist."""
        entity = MockEntity(metadata={"count": 0, "name": "", "active": False})
        assert safe_meta(entity, "count") == 0
        assert safe_meta(entity, "name") == ""
        assert safe_meta(entity, "active") is False

    def test_returns_none_value_not_default(self) -> None:
        """Returns None when that's the actual value, not default."""
        entity = MockEntity(metadata={"explicit_none": None})
        # Key exists with None value - should return None, not default
        result = safe_meta(entity, "explicit_none", "default")
        assert result is None  # metadata.get() returns None

    def test_handles_nested_values(self) -> None:
        """Returns nested dict/list values."""
        entity = MockEntity(metadata={"nested": {"a": 1}, "items": [1, 2, 3]})
        assert safe_meta(entity, "nested") == {"a": 1}
        assert safe_meta(entity, "items") == [1, 2, 3]


# =============================================================================
# safe_attr Tests
# =============================================================================
class TestSafeAttr:
    """Tests for safe_attr function."""

    def test_returns_attribute_when_present(self) -> None:
        """Returns attribute value when it exists."""
        entity = MockEntity(name="direct", metadata={"name": "from_meta"})
        result = safe_attr(entity, "name")
        assert result == "direct"

    def test_falls_back_to_metadata(self) -> None:
        """Falls back to metadata when attribute is None."""
        entity = MockEntity(name=None, metadata={"name": "from_meta"})
        result = safe_attr(entity, "name")
        assert result == "from_meta"

    def test_uses_meta_key_for_fallback(self) -> None:
        """Uses different metadata key when meta_key is specified."""
        entity = MockEntity(status=None, metadata={"agent_status": "working"})
        result = safe_attr(entity, "status", meta_key="agent_status")
        assert result == "working"

    def test_returns_default_when_both_missing(self) -> None:
        """Returns default when attribute and metadata are both missing."""
        entity = MockEntity(category=None, metadata={})
        result = safe_attr(entity, "category", default="unknown")
        assert result == "unknown"

    def test_returns_default_for_list(self) -> None:
        """Returns list default correctly."""
        entity = MockEntity(tags=None, metadata={})
        result = safe_attr(entity, "tags", default=[])
        assert result == []

    def test_attribute_takes_precedence_over_metadata(self) -> None:
        """Attribute value takes precedence even when metadata exists."""
        entity = MockEntity(
            status="from_attr",
            metadata={"status": "from_meta"},
        )
        result = safe_attr(entity, "status")
        assert result == "from_attr"

    def test_handles_missing_attribute(self) -> None:
        """Handles entity without the specified attribute."""
        entity = MockEntity(metadata={"missing_attr": "value"})
        # Entity doesn't have "missing_attr" as an attribute
        result = safe_attr(entity, "missing_attr")
        assert result == "value"  # Falls back to metadata

    def test_default_only_used_when_all_missing(self) -> None:
        """Default is only used when attr and metadata are both missing."""
        entity = MockEntity(
            status="active",
            metadata={"status": "from_meta"},
        )
        result = safe_attr(entity, "status", default="fallback")
        assert result == "active"  # Not "fallback"


# =============================================================================
# has_meta Tests
# =============================================================================
class TestHasMeta:
    """Tests for has_meta function."""

    def test_returns_true_when_truthy(self) -> None:
        """Returns True when metadata key has truthy value."""
        entity = MockEntity(metadata={"active": True, "count": 5})
        assert has_meta(entity, "active") is True
        assert has_meta(entity, "count") is True

    def test_returns_false_when_falsy(self) -> None:
        """Returns False when metadata key has falsy value."""
        entity = MockEntity(metadata={"active": False, "count": 0, "name": ""})
        assert has_meta(entity, "active") is False
        assert has_meta(entity, "count") is False
        assert has_meta(entity, "name") is False

    def test_returns_false_when_missing(self) -> None:
        """Returns False when key is not in metadata."""
        entity = MockEntity(metadata={})
        assert has_meta(entity, "missing") is False

    def test_returns_false_when_none_metadata(self) -> None:
        """Returns False when metadata is None."""
        entity = MockEntity(metadata=None)
        assert has_meta(entity, "key") is False


# =============================================================================
# match_meta Tests
# =============================================================================
class TestMatchMeta:
    """Tests for match_meta function."""

    def test_returns_true_on_match(self) -> None:
        """Returns True when metadata value matches."""
        entity = MockEntity(metadata={"status": "active"})
        assert match_meta(entity, "status", "active") is True

    def test_returns_false_on_mismatch(self) -> None:
        """Returns False when metadata value doesn't match."""
        entity = MockEntity(metadata={"status": "active"})
        assert match_meta(entity, "status", "inactive") is False

    def test_returns_false_when_missing(self) -> None:
        """Returns False when key is missing (None != value)."""
        entity = MockEntity(metadata={})
        assert match_meta(entity, "status", "active") is False

    def test_matches_none_explicitly(self) -> None:
        """Can match None as an explicit value."""
        entity = MockEntity(metadata={"value": None})
        assert match_meta(entity, "value", None) is True

    def test_matches_falsy_values(self) -> None:
        """Matches falsy values correctly."""
        entity = MockEntity(metadata={"count": 0, "name": "", "flag": False})
        assert match_meta(entity, "count", 0) is True
        assert match_meta(entity, "name", "") is True
        assert match_meta(entity, "flag", False) is True


# =============================================================================
# filter_by_meta Tests
# =============================================================================
class TestFilterByMeta:
    """Tests for filter_by_meta function."""

    def test_filter_by_value(self) -> None:
        """Filters entities by exact metadata value match."""
        entities = [
            MockEntity(name="a", metadata={"status": "active"}),
            MockEntity(name="b", metadata={"status": "inactive"}),
            MockEntity(name="c", metadata={"status": "active"}),
        ]
        result = filter_by_meta(entities, "status", "active")
        assert len(result) == 2
        assert all(e.name in ("a", "c") for e in result)

    def test_filter_by_truthy(self) -> None:
        """Filters entities by truthy metadata value when no value specified."""
        entities = [
            MockEntity(name="a", metadata={"archived": True}),
            MockEntity(name="b", metadata={"archived": False}),
            MockEntity(name="c", metadata={}),  # No archived key
        ]
        result = filter_by_meta(entities, "archived")
        assert len(result) == 1
        assert result[0].name == "a"

    def test_filter_exclude(self) -> None:
        """Filters OUT entities that match when exclude=True."""
        entities = [
            MockEntity(name="a", metadata={"archived": True}),
            MockEntity(name="b", metadata={"archived": False}),
            MockEntity(name="c", metadata={}),
        ]
        result = filter_by_meta(entities, "archived", exclude=True)
        # Excludes entities where has_meta("archived") is True
        assert len(result) == 2
        assert all(e.name in ("b", "c") for e in result)

    def test_filter_exclude_by_value(self) -> None:
        """Excludes entities matching specific value."""
        entities = [
            MockEntity(name="a", metadata={"status": "active"}),
            MockEntity(name="b", metadata={"status": "inactive"}),
            MockEntity(name="c", metadata={"status": "active"}),
        ]
        result = filter_by_meta(entities, "status", "active", exclude=True)
        assert len(result) == 1
        assert result[0].name == "b"

    def test_filter_empty_list(self) -> None:
        """Handles empty input list."""
        result = filter_by_meta([], "key", "value")
        assert result == []

    def test_filter_no_matches(self) -> None:
        """Returns empty list when nothing matches."""
        entities = [
            MockEntity(metadata={"status": "a"}),
            MockEntity(metadata={"status": "b"}),
        ]
        result = filter_by_meta(entities, "status", "c")
        assert result == []

    def test_filter_preserves_order(self) -> None:
        """Preserves order of matched entities."""
        entities = [
            MockEntity(name="first", metadata={"ok": True}),
            MockEntity(name="second", metadata={"ok": True}),
            MockEntity(name="third", metadata={"ok": True}),
        ]
        result = filter_by_meta(entities, "ok")
        assert [e.name for e in result] == ["first", "second", "third"]


# =============================================================================
# extract_meta Tests
# =============================================================================
class TestExtractMeta:
    """Tests for extract_meta function."""

    def test_extracts_multiple_keys(self) -> None:
        """Extracts multiple metadata values at once."""
        entity = MockEntity(
            metadata={
                "status": "active",
                "project_id": "proj_123",
                "agent_type": "general",
            }
        )
        result = extract_meta(entity, "status", "project_id", "agent_type")
        assert result == {
            "status": "active",
            "project_id": "proj_123",
            "agent_type": "general",
        }

    def test_uses_defaults_for_missing(self) -> None:
        """Uses defaults dict for missing keys."""
        entity = MockEntity(metadata={"status": "active"})
        result = extract_meta(
            entity,
            "status",
            "project_id",
            defaults={"project_id": "default_project"},
        )
        assert result == {"status": "active", "project_id": "default_project"}

    def test_returns_none_for_missing_without_default(self) -> None:
        """Returns None for missing keys without default."""
        entity = MockEntity(metadata={"status": "active"})
        result = extract_meta(entity, "status", "missing")
        assert result == {"status": "active", "missing": None}

    def test_empty_keys(self) -> None:
        """Returns empty dict when no keys specified."""
        entity = MockEntity(metadata={"key": "value"})
        result = extract_meta(entity)
        assert result == {}

    def test_all_missing(self) -> None:
        """Handles case where all keys are missing."""
        entity = MockEntity(metadata={})
        result = extract_meta(
            entity,
            "a",
            "b",
            "c",
            defaults={"b": "default_b"},
        )
        assert result == {"a": None, "b": "default_b", "c": None}


# =============================================================================
# Integration Tests
# =============================================================================
class TestMetadataIntegration:
    """Integration tests combining multiple utilities."""

    def test_real_world_agent_filtering(self) -> None:
        """Simulates filtering agents by status and type."""

        @dataclass
        class Agent:
            id: str
            name: str
            metadata: dict[str, Any]

        agents = [
            Agent("1", "agent-1", {"status": "working", "agent_type": "general"}),
            Agent("2", "agent-2", {"status": "paused", "agent_type": "general"}),
            Agent("3", "agent-3", {"status": "working", "agent_type": "specialized"}),
            Agent(
                "4", "agent-4", {"status": "terminated", "agent_type": "general", "archived": True}
            ),
        ]

        # Filter active, non-archived agents
        active = filter_by_meta(agents, "archived", exclude=True)
        assert len(active) == 3

        # Filter by status
        working = filter_by_meta(active, "status", "working")
        assert len(working) == 2

        # Extract key metadata
        for agent in working:
            data = extract_meta(
                agent,
                "status",
                "agent_type",
                "project_id",
                defaults={"project_id": "unassigned"},
            )
            assert data["status"] == "working"
            assert data["project_id"] == "unassigned"

    def test_real_world_response_building(self) -> None:
        """Simulates building API response from entity."""

        @dataclass
        class Entity:
            id: str
            name: str
            category: str | None
            tags: list[str] | None
            metadata: dict[str, Any]

        entity = Entity(
            id="entity_123",
            name="Test Entity",
            category=None,  # Will fall back to metadata
            tags=None,  # Will fall back to metadata
            metadata={
                "category": "backend",
                "tags": ["python", "api"],
                "status": "active",
                "created_by": "user_456",
            },
        )

        # Build response using safe accessors
        response = {
            "id": entity.id,
            "name": entity.name,
            "category": safe_attr(entity, "category"),
            "tags": safe_attr(entity, "tags", default=[]),
            "status": safe_meta(entity, "status", "unknown"),
            "created_by": safe_meta(entity, "created_by"),
        }

        assert response == {
            "id": "entity_123",
            "name": "Test Entity",
            "category": "backend",
            "tags": ["python", "api"],
            "status": "active",
            "created_by": "user_456",
        }
