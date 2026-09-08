"""Entity capture sidecars retain authored metadata without trusted provenance."""

from sibyl.api.routes.entity_serialization import sanitize_raw_capture_metadata


def test_entity_capture_metadata_preserves_authored_values_only():
    authored = {"description": "Audit notes", "details": {"source_bindings": "quoted text"}}
    supplied = {
        **authored,
        "principal_id": "caller",
        "raw_memory_id": "source",
        "correction_history": [],
        "source_snapshot_sha256": "digest",
        "source_bindings": {},
        "correction_blockers": {},
        "source_validation_pending": True,
        "lifecycle_reconciliation_pending": True,
    }

    assert sanitize_raw_capture_metadata(supplied) == authored
    assert supplied["source_validation_pending"] is True
