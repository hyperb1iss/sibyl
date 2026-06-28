from __future__ import annotations

from sibyl_core.session_bundle import memory_dedupe_keys, summarize_memory, summarize_raw_memory


def test_summarize_memory_preserves_scope_metadata() -> None:
    summary = summarize_memory(
        {
            "id": "decision_1",
            "name": "Use wake bundles",
            "type": "decision",
            "source": "northstar",
            "content": "Wake bundles should stay tiny.",
            "metadata": {
                "document_id": "doc_1",
                "memory_scope": "project",
                "scope_key": "project_123",
            },
        }
    )

    assert summary == {
        "id": "decision_1",
        "name": "Use wake bundles",
        "entity_type": "decision",
        "source": "northstar",
        "preview": "Wake bundles should stay tiny.",
        "document_id": "doc_1",
        "memory_scope": "project",
        "scope_key": "project_123",
    }


def test_summarize_raw_memory_uses_raw_memory_identity_and_source() -> None:
    summary = summarize_raw_memory(
        {
            "id": "raw_1",
            "title": "Private handoff",
            "raw_content": "[Note] Remember the handoff before coding.",
            "source_id": "cli:manual",
            "memory_scope": "private",
            "scope_key": None,
        }
    )

    assert summary == {
        "id": "raw_memory:raw_1",
        "name": "Private handoff",
        "entity_type": "raw_memory",
        "source": "cli:manual",
        "preview": "Remember the handoff before coding.",
        "document_id": None,
        "memory_scope": "private",
        "scope_key": None,
    }


def test_memory_dedupe_keys_match_duplicate_summaries_with_different_ids() -> None:
    first = {
        "id": "raw_memory:one",
        "name": "HA deploy runbook",
        "preview": "Deploy via GitHub, then pull on the box.",
    }
    second = {
        "id": "raw_memory:two",
        "name": "HA deploy runbook",
        "preview": "Deploy via GitHub, then pull on the box.",
    }

    assert memory_dedupe_keys(first) & memory_dedupe_keys(second)
