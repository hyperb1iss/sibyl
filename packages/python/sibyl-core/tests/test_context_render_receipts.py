"""Visible evidence receipts bind exact output to the selected source snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import field, replace
from pathlib import Path

import pytest

from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextItemQualityMetadata,
    ContextPack,
    ContextRelatedItem,
    ContextSection,
)
from sibyl_core.tools.context import (
    _item_from_result,
    context_pack_to_markdown,
    render_context_pack,
)
from sibyl_core.tools.responses import SearchResult


def receipt_pack() -> ContextPack:
    items = [
        ContextItem(
            id=f"record-{index}",
            type="note",
            name=f"Memory 💜 {index}",
            content=" \n αβ  repeated\ttext\n" * (index + 1) * 15,
            score=1 - index / 20,
            facet=ContextFacet.DECISIONS,
            reason="test",
            source="alias-is-not-record",
            source_revision=7,
            quality=ContextItemQualityMetadata(updated_at="2026-09-05T02:04:00Z"),
            related=[
                ContextRelatedItem(
                    "related-1", "note", "邻居💜", "DERIVED_FROM", "outgoing", source_revision=3
                )
            ],
        )
        for index in range(7)
    ]
    return ContextPack(
        goal="Unicode 💜 receipt",
        intent=ContextIntent.GENERAL,
        query="repeated text",
        domain=None,
        project="project-1",
        sections=[
            ContextSection(ContextFacet.DECISIONS, "Decisions", items[:4]),
            ContextSection(ContextFacet.PRIOR_ART, "Prior Art", items[4:]),
        ],
        total_items=len(items),
    )


@pytest.mark.parametrize("budget", [None, 100, 250, 500, 1000, 4000])
@pytest.mark.parametrize("include_related", [False, True])
def test_receipt_matches_frozen_pre_receipt_markdown(budget, include_related):
    expected = json.loads(
        (Path(__file__).parent / "fixtures/context_render_markdown.json").read_text()
    )
    result = render_context_pack(
        receipt_pack(), token_budget=budget, include_related=include_related
    )
    key = f"{budget}:{include_related}"
    assert result.markdown == expected[key]
    assert (
        context_pack_to_markdown(
            receipt_pack(), token_budget=budget, include_related=include_related
        )
        == expected[key]
    )
    assert result.receipt.markdown_sha256 == hashlib.sha256(result.markdown.encode()).hexdigest()
    assert result.receipt.markdown_bytes == len(result.markdown.encode())


@pytest.mark.parametrize("budget", [None, 100, 500, 4000])
def test_each_field_span_reconstructs_exact_utf8_output(budget):
    pack = receipt_pack()
    result = render_context_pack(pack, token_budget=budget)
    output = result.markdown.encode()
    for span in result.receipt.spans:
        assert 0 <= span.start_byte <= span.end_byte <= len(output)
        visible = output[span.start_byte : span.end_byte].decode()
        item = pack.items[span.item_index]
        if span.field == "item":
            assert visible.startswith(f"- **{item.name}**")
            assert span.record_id == item.id
            continue
        if span.field == "related.name":
            source = item.related[0].name
            assert span.record_id == "related-1"
            assert span.source_revision == 3
        else:
            source = getattr(item, span.field)
            assert span.record_id == item.id
            assert span.source_alias == "alias-is-not-record"
            assert span.source_revision == 7
        assert hashlib.sha256(source.encode()).hexdigest() == span.input_sha256
        selected = source.encode()[span.input_start_byte : span.input_end_byte].decode()
        assert (
            " ".join(selected.split()) if span.transform == "collapse_whitespace" else selected
        ) == visible
    emitted = {span.item_index for span in result.receipt.spans}
    assert len(result.receipt.dispositions) == len(pack.items)
    for disposition in result.receipt.dispositions:
        assert (disposition.state != "omitted") == (disposition.item_index in emitted)


def test_unknown_revision_is_not_recovered_from_untrusted_metadata():
    result = SearchResult(
        "record",
        "note",
        "title",
        "body",
        1,
        source="alias",
        metadata={"revision": 99, "source_revision": 88},
    )
    item = _item_from_result(result, ContextFacet.DECISIONS, audit=True)
    pack = receipt_pack()
    pack = replace(
        pack, sections=[ContextSection(ContextFacet.DECISIONS, "D", [item])], total_items=1
    )
    rendered = render_context_pack(pack)
    assert all(
        span.source_revision is None and span.revision_status == "unavailable"
        for span in rendered.receipt.spans
    )
    assert all(span.record_id == "record" for span in rendered.receipt.spans)


@pytest.mark.parametrize("include_related", [False, True])
def test_related_spans_follow_filtered_capped_unicode_neighbors(include_related):
    neighbors = [
        ContextRelatedItem("project-0", "project", "Hidden project", "BELONGS_TO", "outgoing"),
        ContextRelatedItem("first", "note", "邻居💜; repeated", "DERIVED_FROM", "outgoing"),
        ContextRelatedItem("project-1", "project", "Other project", "BELONGS_TO", "outgoing"),
        ContextRelatedItem("second", "note", "邻居💜\nsecond", "RELATED_TO", "incoming"),
        ContextRelatedItem("third", "project", "Visible αβ", "RELATED_TO", "outgoing"),
        ContextRelatedItem("fourth", "note", "Capped fourth", "DERIVED_FROM", "outgoing"),
    ]
    pack = receipt_pack()
    item = replace(pack.items[0], related=neighbors)
    pack = replace(
        pack, sections=[ContextSection(ContextFacet.DECISIONS, "D", [item])], total_items=1
    )
    rendered = render_context_pack(pack, include_related=include_related)
    spans = [span for span in rendered.receipt.spans if span.field == "related.name"]
    assert len(rendered.receipt.dispositions) == 1
    assert rendered.receipt.dispositions[0].record_id == item.id
    if not include_related:
        assert spans == []
        assert "  - Related: " not in rendered.markdown
        return

    selected = [neighbors[1], neighbors[3], neighbors[4]]
    assert [span.record_id for span in spans] == [neighbor.id for neighbor in selected]
    expected_line = "  - Related: " + "; ".join(
        f"{neighbor.relationship} {neighbor.name} ({neighbor.type})" for neighbor in selected
    )
    assert expected_line in rendered.markdown
    output = rendered.markdown.encode()
    offset = output.index(expected_line.encode()) + len(b"  - Related: ")
    for position, (span, neighbor) in enumerate(zip(spans, selected, strict=True)):
        if position:
            offset += len(b"; ")
        offset += len((neighbor.relationship + " ").encode())
        assert span.start_byte == offset
        assert span.end_byte == offset + len(neighbor.name.encode())
        assert output[span.start_byte : span.end_byte].decode() == neighbor.name
        assert span.input_sha256 == hashlib.sha256(neighbor.name.encode()).hexdigest()
        offset = span.end_byte + len(f" ({neighbor.type})".encode())
    assert "Hidden project" not in rendered.markdown
    assert "Other project" not in rendered.markdown
    assert "Capped fourth" not in rendered.markdown


def test_v1_quality_digest_ignores_future_dataclass_fields(monkeypatch):
    pack = receipt_pack()
    original = render_context_pack(pack)
    monkeypatch.setattr(
        ContextItemQualityMetadata,
        "__dataclass_fields__",
        {**ContextItemQualityMetadata.__dataclass_fields__, "future_field": field(default=None)},
    )
    assert render_context_pack(pack) == original


def test_repeated_item_objects_keep_distinct_selected_occurrences():
    pack = receipt_pack()
    item = pack.items[0]
    pack = replace(
        pack, sections=[ContextSection(ContextFacet.DECISIONS, "D", [item, item])], total_items=2
    )
    rendered = render_context_pack(pack)
    assert {span.item_index for span in rendered.receipt.spans} == {0, 1}
    assert all(disposition.state != "omitted" for disposition in rendered.receipt.dispositions)


@pytest.mark.parametrize(
    "content,state",
    [
        ("", "unavailable"),
        ("same", "same_as_name"),
        ("\t \n", "unavailable"),
        ("complete", "emitted"),
    ],
)
def test_content_absence_is_not_budget_trimming(content, state):
    pack = receipt_pack()
    item = replace(pack.items[0], name="same", content=content, related=[])
    pack = replace(
        pack, sections=[ContextSection(ContextFacet.DECISIONS, "D", [item])], total_items=1
    )
    result = render_context_pack(pack)
    assert result.receipt.dispositions[0].content_state == state
    assert result.receipt.dispositions[0].reason == "rendered"


def test_related_body_and_omitted_neighbors_are_not_claimed_visible():
    pack = receipt_pack()
    item = replace(
        pack.items[0],
        related=[
            ContextRelatedItem(
                "private-not-admitted", "project", "Hidden", "BELONGS_TO", "outgoing"
            )
        ],
    )
    pack = replace(
        pack, sections=[ContextSection(ContextFacet.DECISIONS, "D", [item])], total_items=1
    )
    rendered = render_context_pack(pack)
    assert "private-not-admitted" not in json.dumps(
        rendered.receipt, default=lambda value: value.__dict__
    )
    assert not any(span.field == "related.name" for span in rendered.receipt.spans)


def test_receipt_validates_after_json_transport_and_rejects_tampering():
    from dataclasses import asdict

    from sibyl_core.tools.context import validate_context_render_payload

    pack = receipt_pack()
    rendered = render_context_pack(pack, token_budget=500, request_id="request-123")
    payload = json.loads(
        json.dumps(
            {
                **asdict(pack),
                "markdown": rendered.markdown,
                "render_receipt": asdict(rendered.receipt),
            }
        )
    )
    assert validate_context_render_payload(payload) == []
    mutations = [
        lambda p: p.update(markdown=p["markdown"] + "extra"),
        lambda p: p["render_receipt"].update(spans=[]),
        lambda p: p["render_receipt"].update(dispositions=[]),
        lambda p: p["render_receipt"]["spans"][0].update(record_id="other"),
        lambda p: p["render_receipt"]["spans"][0].update(start_byte=1),
        lambda p: p["render_receipt"]["spans"][0].update(source_revision=8),
        lambda p: p["sections"][0]["items"][0].update(content="changed source snapshot"),
    ]
    for mutate in mutations:
        altered = json.loads(json.dumps(payload))
        mutate(altered)
        assert validate_context_render_payload(altered)
    assert validate_context_render_payload({"markdown": "historical"}) == []


def test_authorized_revisions_override_metadata_in_both_candidate_adapters():
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.retrieval._search_candidates import _candidate_from_node_record
    from sibyl_core.retrieval._search_plan import RetrievalSignal
    from sibyl_core.tools.search import graph_entity_to_search_result

    entity = Entity(
        id="record",
        name="note",
        entity_type=EntityType.NOTE,
        revision=4,
        observed_revision=4,
        metadata={"revision": 99, "source_revision": 99},
    )
    result = graph_entity_to_search_result(
        entity, organization_id="org", principal_id="user", score=1
    )
    assert result.source_revision == 4
    item = _item_from_result(result, ContextFacet.DECISIONS)
    assert item.source_revision == 4
    for value, expected in [(5, 5), (None, None), (True, None), ("5", None)]:
        candidate = _candidate_from_node_record(
            {
                "uuid": "record",
                "entity_type": "note",
                "revision": value,
                "attributes": {"revision": 99},
            },
            signal=RetrievalSignal.NODE_FULLTEXT,
            score=1,
        )
        assert candidate.source_revision == expected


def test_raw_capture_revision_survives_both_authorized_adapters():
    from sibyl_core.retrieval._search_candidates import _candidate_from_raw_memory
    from sibyl_core.retrieval._search_plan import ScopeSpec
    from sibyl_core.services.surreal_content import MemoryScope, RawMemory
    from sibyl_core.tools.search import _raw_memory_search_result

    memory = RawMemory(
        "capture",
        "org",
        "source-alias",
        "owner",
        revision=6,
        observed_revision=6,
        raw_content="captured",
        metadata={"revision": 91},
    )
    standard = _raw_memory_search_result(
        memory, organization_id="org", include_content=True, content_max_chars=100
    )
    native = _candidate_from_raw_memory(
        memory, ScopeSpec(MemoryScope.PRIVATE, "owner", "authorized", "owner")
    )
    assert standard.source_revision == native.source_revision == 6
    assert standard.id == native.id == "raw_memory:capture"


def test_legacy_empty_pack_is_distinct_from_missing_provenance():
    from dataclasses import asdict

    from sibyl_core.tools.context import validate_context_render_payload

    pack = replace(receipt_pack(), sections=[], total_items=0)
    rendered = render_context_pack(pack)
    assert rendered.receipt.spans == rendered.receipt.dispositions == []
    assert rendered.receipt.selected_items == 0
    assert (
        validate_context_render_payload(
            {
                **asdict(pack),
                "markdown": rendered.markdown,
                "render_receipt": asdict(rendered.receipt),
            }
        )
        == []
    )


@pytest.mark.parametrize(
    "body", ["💜" * 95, "x" * 80 + "...", "word\t\n" * 70, "αβ " * 42 + "suffix..."]
)
def test_hard_cutoffs_keep_generated_ellipsis_outside_source_spans(body):
    pack = receipt_pack()
    item = replace(pack.items[0], content=body, related=[])
    pack = replace(
        pack, sections=[ContextSection(ContextFacet.DECISIONS, "D", [item])], total_items=1
    )
    result = render_context_pack(pack, max_content_chars=80)
    span = next(span for span in result.receipt.spans if span.field == "content")
    output = result.markdown.encode()
    assert output[span.end_byte : span.end_byte + 3] == b"..."
    source = body.encode()[span.input_start_byte : span.input_end_byte].decode()
    assert " ".join(source.split()).encode() == output[span.start_byte : span.end_byte]


@pytest.mark.parametrize(
    "stored, expected", [(None, None), (1, 1), (4, 4), (True, None), ("4", None)]
)
@pytest.mark.parametrize("entity_type", ["note", "task", "procedure"])
def test_legacy_decoder_default_is_not_an_observed_revision(stored, expected, entity_type):
    from sibyl_core.services.content_models import raw_memory_from_record
    from sibyl_core.services.graph_records import entity_from_surreal_row
    from sibyl_core.tools.search import graph_entity_to_search_result

    row = {
        "uuid": "record",
        "name": "name",
        "entity_type": entity_type,
        "revision": stored,
        "attributes": {"revision": 999, "observed_revision": 888},
    }
    entity = entity_from_surreal_row(row)
    assert entity.revision >= 1
    assert entity.observed_revision == expected
    assert "observed_revision" not in entity.model_dump()
    result = graph_entity_to_search_result(
        entity, organization_id="org", principal_id="owner", score=1
    )
    assert result.source_revision == expected
    raw = raw_memory_from_record({**row, "metadata": row["attributes"], "memory_scope": "private"})
    assert raw.revision >= 1
    assert raw.observed_revision == expected


@pytest.mark.asyncio
async def test_stored_revision_readback_ignores_authored_provenance_fields():
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.graph import EntityManager
    from sibyl_core.services.graph_client import SurrealGraphClient
    from sibyl_core.tools.search import graph_entity_to_search_result

    client = SurrealGraphClient(group_id="receipt-observed-revision", url="memory://")
    try:
        manager = EntityManager(client, group_id=client.group_id)
        await manager.create_direct(
            Entity(
                id="observed",
                entity_type=EntityType.NOTE,
                name="Stored note",
                content="Actual snapshot",
                revision=1,
                observed_revision=999,
                metadata={"observed_revision": 888, "revision": 777},
            )
        )
        current = await manager.get("observed")
        assert current.observed_revision == current.revision == 1
        result = graph_entity_to_search_result(
            current, organization_id=client.group_id, principal_id="owner", score=1
        )
        assert result.source_revision == 1
        rows = await client.execute_query(
            "SELECT revision, observed_revision FROM entity WHERE uuid = $uuid;", uuid="observed"
        )
        assert rows == [{"revision": 1, "observed_revision": None}]
    finally:
        await client.close()


def test_raw_observed_revision_is_not_an_authored_storage_field():
    from sibyl_core.services.content_models import (
        RawMemory,
        raw_memory_from_record,
        raw_memory_record,
    )

    memory = RawMemory("capture", "org", "source", "owner", revision=2, observed_revision=999)
    record = raw_memory_record(memory)
    assert "observed_revision" not in record
    assert raw_memory_from_record(record).observed_revision == 2
