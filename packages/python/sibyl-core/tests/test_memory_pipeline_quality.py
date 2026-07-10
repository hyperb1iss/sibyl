from sibyl_core.memory_pipeline.quality import (
    expand_memory_quality_storage_metadata,
    memory_metadata_score,
    normalize_memory_quality_metadata,
)


def test_normalize_memory_quality_metadata_preserves_legacy_precedence() -> None:
    normalized = normalize_memory_quality_metadata(
        {
            "importance": 0.8,
            "retention_importance": 0.3,
            "confidence": 0.9,
            "reflection_confidence": 0.4,
            "title": "keep me",
        }
    )

    assert normalized == {
        "importance": 0.3,
        "confidence": 0.4,
        "title": "keep me",
    }


def test_normalize_memory_quality_metadata_skips_invalid_higher_priority_scores() -> None:
    normalized = normalize_memory_quality_metadata(
        {
            "retention_importance": "bogus",
            "importance": 0.8,
            "promotion_confidence": False,
            "reflection_confidence": "unknown",
            "confidence": 0.9,
        }
    )

    assert normalized == {"importance": 0.8, "confidence": 0.9}


def test_normalize_memory_quality_metadata_migrates_legacy_scores() -> None:
    normalized = normalize_memory_quality_metadata(
        {
            "memory_importance": "1.4",
            "projection_confidence": "0.72",
        }
    )

    assert normalized == {"importance": 1.0, "confidence": 0.72}


def test_memory_metadata_score_rejects_booleans_and_invalid_values() -> None:
    assert memory_metadata_score({"confidence": True}, "confidence") is None
    assert memory_metadata_score({"confidence": "unknown"}, "confidence") is None
    assert memory_metadata_score({"confidence": "nan"}, "confidence") is None


def test_expand_memory_quality_storage_metadata_dual_writes_rolling_shadows() -> None:
    expanded = expand_memory_quality_storage_metadata(
        {"importance": 0.8, "confidence": 0.9, "title": "keep me"}
    )

    assert expanded["importance"] == 0.8
    assert expanded["confidence"] == 0.9
    assert expanded["retention_importance"] == 0.8
    assert expanded["memory_importance"] == 0.8
    assert expanded["promotion_confidence"] == 0.9
    assert expanded["reflection_confidence"] == 0.9
    assert expanded["projection_confidence"] == 0.9
    assert expanded["share_confidence"] == 0.9
    assert expanded["title"] == "keep me"
