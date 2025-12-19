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
