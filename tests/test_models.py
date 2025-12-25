"""Tests for Pydantic models."""

from sibyl.models.entities import (
    Entity,
    EntityType,
    Pattern,
    Relationship,
    RelationshipType,
    Rule,
)


class TestEntityModels:
    """Tests for entity models."""

    def test_pattern_creation(self, sample_pattern: dict[str, object]) -> None:
        """Test creating a Pattern entity."""
        pattern = Pattern(**sample_pattern)
        assert pattern.entity_type == EntityType.PATTERN
        assert pattern.name == "Error Boundary Pattern"
        assert "python" in pattern.languages

    def test_rule_creation(self, sample_rule: dict[str, object]) -> None:
        """Test creating a Rule entity."""
        rule = Rule(**sample_rule)
        assert rule.entity_type == EntityType.RULE
        assert rule.severity == "error"

    def test_entity_defaults(self) -> None:
        """Test default values on Entity."""
        entity = Entity(id="test-001", entity_type=EntityType.TOPIC, name="Test Topic")
        assert entity.description == ""
        assert entity.content == ""
        assert entity.metadata == {}
        assert entity.source_file is None


class TestRelationshipModels:
    """Tests for relationship models."""

    def test_relationship_creation(self) -> None:
        """Test creating a Relationship."""
        rel = Relationship(
            id="rel-001",
            relationship_type=RelationshipType.APPLIES_TO,
            source_id="pattern-001",
            target_id="language-python",
        )
        assert rel.relationship_type == RelationshipType.APPLIES_TO
        assert rel.weight == 1.0

    def test_relationship_weight_bounds(self) -> None:
        """Test relationship weight validation."""
        rel = Relationship(
            id="rel-001",
            relationship_type=RelationshipType.RELATED_TO,
            source_id="a",
            target_id="b",
            weight=0.5,
        )
        assert rel.weight == 0.5


class TestCypherInjectionPrevention:
    """Tests for Cypher injection prevention in relationship queries."""

    def test_validate_relationship_type_valid(self) -> None:
        """Test that valid relationship types pass validation."""
        from sibyl.graph.relationships import _validate_relationship_type

        # All enum values should be valid
        for rel_type in RelationshipType:
            result = _validate_relationship_type(rel_type.value)
            assert result == rel_type.value

    def test_validate_relationship_type_injection_attempt(self) -> None:
        """Test that injection attempts are rejected."""
        import pytest

        from sibyl.graph.relationships import _validate_relationship_type

        # Common injection patterns
        injection_attempts = [
            "RELATES_TO]->(x) DELETE x//",
            "RELATES_TO}]->(x)//",
            "'; DROP DATABASE;--",
            "UNION SELECT * FROM users",
            "RELATED_TO` DETACH DELETE n //",
        ]

        for attempt in injection_attempts:
            with pytest.raises(ValueError, match="Invalid relationship type"):
                _validate_relationship_type(attempt)

    def test_sanitize_pagination_valid(self) -> None:
        """Test that valid pagination values are passed through."""
        from sibyl.graph.relationships import _sanitize_pagination

        assert _sanitize_pagination(0) == 0
        assert _sanitize_pagination(10) == 10
        assert _sanitize_pagination(100) == 100

    def test_sanitize_pagination_negative(self) -> None:
        """Test that negative values are clamped to 0."""
        from sibyl.graph.relationships import _sanitize_pagination

        assert _sanitize_pagination(-1) == 0
        assert _sanitize_pagination(-100) == 0

    def test_sanitize_pagination_exceeds_max(self) -> None:
        """Test that values exceeding max are clamped."""
        from sibyl.graph.relationships import _sanitize_pagination

        # Default max is 10000
        assert _sanitize_pagination(50000) == 10000
        # Custom max
        assert _sanitize_pagination(500, max_value=100) == 100

    def test_sanitize_pagination_type_error(self) -> None:
        """Test that non-integer values raise TypeError."""
        import pytest

        from sibyl.graph.relationships import _sanitize_pagination

        with pytest.raises(TypeError, match="must be int"):
            _sanitize_pagination("10")  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="must be int"):
            _sanitize_pagination(10.5)  # type: ignore[arg-type]
