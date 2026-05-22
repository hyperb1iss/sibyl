from sibyl_core.audit import audit_event_matches_resource, audit_event_resource


def test_audit_event_resource_prefers_explicit_resource() -> None:
    row = {
        "action": "api_key.create",
        "details": {
            "resource": "api_key:abc",
            "project_id": "project-1",
        },
    }

    assert audit_event_resource(row) == "api_key:abc"


def test_audit_event_matches_nested_resource_details() -> None:
    row = {
        "uuid": "audit-1",
        "action": "memory.remember",
        "details": {
            "source_ids": ["memory-source-1"],
            "details": {"task_id": "task-123"},
        },
    }

    assert audit_event_matches_resource(row, "task-123") is True
    assert audit_event_matches_resource(row, "missing") is False
