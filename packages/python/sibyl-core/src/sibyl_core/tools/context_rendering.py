"""Render selected context snapshots and account for their exact visible evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from pydantic import TypeAdapter, ValidationError

from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextPack,
    ContextRelatedItem,
    ContextRenderDisposition,
    ContextRenderOptions,
    ContextRenderReceipt,
    ContextRenderSpan,
    ContextSection,
    RenderedContextPack,
)


def _compact_metadata_value(value: Any, max_chars: int = 120) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    elif isinstance(value, bool | int | float):
        value = str(value)
    elif not isinstance(value, str):
        return None

    compact = " ".join(value.strip().split())
    if not compact:
        return None
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _render_content(item: ContextItem, max_chars: int) -> tuple[str | None, str]:
    """Choose the emitted body and its disposition together."""
    if not item.content:
        return None, "unavailable"
    if item.content.strip() == (item.name or "").strip():
        return None, "same_as_name" if item.content.strip() else "unavailable"
    compact = " ".join(item.content.strip().split())
    if len(compact) <= max_chars:
        return compact, "emitted" if compact else "unavailable"
    cutoff = compact.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return compact[:cutoff].rstrip() + "...", "truncated"


def _quality_value(quality: Any, key: str) -> str | None:
    if isinstance(quality, dict):
        return _compact_metadata_value(quality.get(key))
    return _compact_metadata_value(getattr(quality, key, None))


def _date_only(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
        return value[:10]
    return value


def _quality_metadata_to_markdown(
    quality: Any,
    *,
    item_id: str | None = None,
    pack_project: str | None = None,
) -> str:
    """Render provenance that adds signal, skipping values the pack already states."""

    parts: list[str] = []
    origin = _quality_value(quality, "origin")
    if origin and origin != "graph":
        parts.append(origin)
    source = _quality_value(quality, "source")
    if source and source != item_id:
        parts.append(f"src={source}")
    project_id = _quality_value(quality, "project_id")
    if project_id and project_id != pack_project:
        parts.append(f"project={project_id}")
    if updated_at := _date_only(_quality_value(quality, "updated_at")):
        parts.append(f"updated={updated_at}")
    elif created_at := _date_only(_quality_value(quality, "created_at")):
        parts.append(f"created={created_at}")
    if valid_at := _date_only(_quality_value(quality, "valid_at")):
        parts.append(f"valid={valid_at}")
    if url := _quality_value(quality, "url"):
        parts.append(f"url={url}")
    return "; ".join(parts)


_MARKDOWN_CHARS_PER_TOKEN = 4
# What an unbudgeted product request should mean. A pack retrieved from a real
# graph carries several times more content than the count defaults render, and
# the discarded remainder was already paid for by search and ranking, so a
# request that states no constraint gets a budget rather than a stub. Library
# callers keep `None` so measurement baselines rendered by counts still are.
DEFAULT_MARKDOWN_TOKEN_BUDGET = 4_000
_MARKDOWN_ITEM_CEILING = 50
_MARKDOWN_SECTION_ITEM_CEILING = 10
_MARKDOWN_CONTENT_CEILING = 1200
_BUDGET_CONTENT_SHARE = 0.85
_ACTIVE_WORK_ANCHOR_INTENTS = frozenset({ContextIntent.BUILD, ContextIntent.GENERAL})


def _has_active_lookup(section: ContextSection) -> bool:
    return section.facet is ContextFacet.ACTIVE_WORK and any(
        item.metadata.get("active_lookup") for item in section.items
    )


def _section_render_score(section: ContextSection) -> float:
    score = max((item.score for item in section.items), default=0.0)
    if section.facet is ContextFacet.RECENT_MEMORY:
        score += 0.25
    elif _has_active_lookup(section):
        score = max(score, 0.75)
    return score


def _section_render_key(
    section: ContextSection,
    *,
    index: int,
    intent: ContextIntent,
) -> tuple[int, float, int]:
    if intent in _ACTIVE_WORK_ANCHOR_INTENTS and _has_active_lookup(section):
        return (0, 0.0, index)
    return (1, -_section_render_score(section), index)


def _sections_for_markdown(
    sections: Sequence[ContextSection],
    *,
    intent: ContextIntent,
) -> list[ContextSection]:
    return [
        section
        for _key, section in sorted(
            (
                (_section_render_key(section, index=index, intent=intent), section)
                for index, section in enumerate(sections)
            ),
            key=lambda item: item[0],
        )
    ]


@dataclass(frozen=True)
class _RenderedItem:
    lines: list[str]
    related: list[ContextRelatedItem]
    content_state: str


def _render_item(
    item: ContextItem,
    *,
    pack_project: str | None,
    max_content_chars: int,
    include_related: bool,
) -> _RenderedItem:
    status = _compact_metadata_value(item.metadata.get("status"))
    if item.type and status:
        type_label = f" ({item.type} · {status})"
    elif item.type:
        type_label = f" ({item.type})"
    else:
        type_label = ""
    item_quality = getattr(item, "quality", item.metadata.get("quality", {}))
    quality = _quality_metadata_to_markdown(
        item_quality,
        item_id=item.id,
        pack_project=pack_project,
    )
    quality_label = f" _{quality}_" if quality else ""
    lines = [f"- **{item.name}**{type_label} `{item.id}`{quality_label}"]
    content, content_state = _render_content(item, max_content_chars)
    if content is not None:
        lines.append(f"  - Memory: {content}")
    related = (
        [
            candidate
            for candidate in item.related
            if not (candidate.relationship == "BELONGS_TO" and candidate.type == "project")
        ][:3]
        if include_related
        else []
    )
    if related:
        labels = "; ".join(
            f"{candidate.relationship} {candidate.name} ({candidate.type})" for candidate in related
        )
        lines.append(f"  - Related: {labels}")
    return _RenderedItem(lines, related, content_state)


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _compact_source_end(source: str, visible: str) -> int:
    """Map a whitespace-collapsed prefix back to its source character boundary."""
    consumed = 0
    for match in re.finditer(r"\S+", source):
        if consumed:
            consumed += 1
        remaining = len(visible) - consumed
        if remaining <= len(match.group()):
            return match.start() + max(remaining, 0)
        consumed += len(match.group())
    return len(source.rstrip())


# Persisted v1 receipts bind this projection; unrelated model fields must not change it.
_V1_QUALITY_DIGEST_KEYS = (
    "origin",
    "source",
    "url",
    "created_at",
    "updated_at",
    "valid_at",
    "project_id",
)


def _item_render_spans(
    item: ContextItem,
    rendered_item: _RenderedItem,
    *,
    index: int,
    start: int,
) -> list[ContextRenderSpan]:
    revision = (
        item.source_revision
        if type(item.source_revision) is int and item.source_revision > 0
        else None
    )
    lines = rendered_item.lines
    block = "\n".join(lines)
    snapshot = json.dumps(
        {
            "id": item.id,
            "name": item.name,
            "type": item.type,
            "content": item.content,
            "status": _compact_metadata_value(item.metadata.get("status")),
            "quality": {key: _quality_value(item.quality, key) for key in _V1_QUALITY_DIGEST_KEYS},
            "related": [
                {
                    "id": related.id,
                    "name": related.name,
                    "type": related.type,
                    "relationship": related.relationship,
                }
                for related in item.related
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    common: dict[str, Any] = {
        "item_index": index,
        "record_id": item.id,
        "record_type": item.type,
        "source_alias": item.source,
        "source_revision": revision,
        "revision_status": "available" if revision is not None else "unavailable",
    }
    spans = [
        ContextRenderSpan(
            **common,
            field="item",
            start_byte=start,
            end_byte=start + len(block.encode()),
            input_sha256=_text_digest(snapshot),
            transform="markdown_item_v1",
        )
    ]
    name_start = start + len(b"- **")
    spans.append(
        ContextRenderSpan(
            **common,
            field="name",
            start_byte=name_start,
            end_byte=name_start + len(item.name.encode()),
            input_sha256=_text_digest(item.name),
            transform="identity",
            input_start_byte=0,
            input_end_byte=len(item.name.encode()),
        )
    )
    if len(lines) > 1 and lines[1].startswith("  - Memory: "):
        rendered = lines[1].removeprefix("  - Memory: ")
        visible = rendered[:-3] if rendered_item.content_state == "truncated" else rendered
        source_end = _compact_source_end(item.content, visible)
        body_start = start + len((lines[0] + "\n  - Memory: ").encode())
        spans.append(
            ContextRenderSpan(
                **common,
                field="content",
                start_byte=body_start,
                end_byte=body_start + len(visible.encode()),
                input_sha256=_text_digest(item.content),
                transform="collapse_whitespace",
                input_start_byte=0,
                input_end_byte=len(item.content[:source_end].encode()),
            )
        )
    if lines[-1].startswith("  - Related: "):
        related_start = start + len(("\n".join(lines[:-1]) + "\n  - Related: ").encode())
        for position, candidate in enumerate(rendered_item.related):
            if position:
                related_start += len(b"; ")
            related_start += len((candidate.relationship + " ").encode())
            candidate_revision = (
                candidate.source_revision
                if type(candidate.source_revision) is int and candidate.source_revision > 0
                else None
            )
            spans.append(
                ContextRenderSpan(
                    item_index=index,
                    record_id=candidate.id,
                    record_type=candidate.type,
                    source_alias=None,
                    source_revision=candidate_revision,
                    revision_status="available"
                    if candidate_revision is not None
                    else "unavailable",
                    field="related.name",
                    start_byte=related_start,
                    end_byte=related_start + len(candidate.name.encode()),
                    input_sha256=_text_digest(candidate.name),
                    transform="identity",
                    input_start_byte=0,
                    input_end_byte=len(candidate.name.encode()),
                )
            )
            related_start += len((candidate.name + " (" + candidate.type + ")").encode())
    return spans


def render_context_pack(
    pack: ContextPack,
    *,
    max_items: int = 8,
    items_per_section: int = 3,
    max_content_chars: int = 280,
    include_related: bool = True,
    token_budget: int | None = None,
    request_id: str | None = None,
) -> RenderedContextPack:
    """Render a context pack as compact Markdown for agent injection.

    token_budget is the caller's real constraint, so it decides the pack rather
    than trimming one already sized by counts. Given a budget, breadth and
    depth both scale toward it: more of the pack's items render, and each is
    allowed more of its content, up to the hard ceilings. The per-block guard
    below is what keeps the result inside the budget, and it always emits at
    least one item so a tight budget degrades to a minimal brief rather than an
    empty pack. Without a budget the count defaults bind exactly as they always
    have, because there is nothing to size against.
    """

    options = ContextRenderOptions(
        max_items, items_per_section, max_content_chars, include_related, token_budget
    )
    max_items = max(1, min(max_items, _MARKDOWN_ITEM_CEILING))
    items_per_section = max(1, min(items_per_section, _MARKDOWN_SECTION_ITEM_CEILING))
    max_content_chars = max(80, min(max_content_chars, _MARKDOWN_CONTENT_CEILING))
    char_budget = (
        max(400, token_budget * _MARKDOWN_CHARS_PER_TOKEN) if token_budget is not None else None
    )
    if char_budget is not None:
        renderable = max(1, pack.total_items)
        max_items = min(_MARKDOWN_ITEM_CEILING, max(max_items, renderable))
        # Each section may claim its share of the item allowance. A fixed
        # per-section cap would leave the budget unspent whenever the pack's
        # items concentrate in few facets, which is the same slot rationing the
        # budget exists to replace, while a share still spreads a wide pack
        # across its facets instead of letting the first one take everything.
        section_count = max(1, len(pack.sections))
        fair_share = -(-max_items // section_count)
        items_per_section = max(items_per_section, fair_share)
        # Reserve a share for headers, titles, and related lines so the content
        # allowance does not budget for text the block has to carry anyway.
        content_allowance = int(char_budget * _BUDGET_CONTENT_SHARE) // min(max_items, renderable)
        max_content_chars = min(
            _MARKDOWN_CONTENT_CEILING, max(max_content_chars, content_allowance)
        )

    lines = [
        f"# Sibyl Context Pack: {pack.goal}",
        f"Intent: {pack.intent.value}",
        f"Layer: {pack.layer.value}",
        f"Query: {pack.query}",
    ]
    if pack.domain:
        lines.append(f"Domain: {pack.domain}")
    if pack.project:
        lines.append(f"Project: {pack.project}")

    spans: list[ContextRenderSpan] = []
    section_offsets: dict[int, list[int]] = {}
    offset = 0
    for section in pack.sections:
        section_offsets.setdefault(id(section), []).append(offset)
        offset += len(section.items)
    dispositions = {
        index: ContextRenderDisposition(index, item.id, "omitted", "item_limit", "not_emitted")
        for index, item in enumerate(pack.items)
    }
    used = sum(len(line) + 1 for line in lines)
    used_bytes = sum(len(line.encode()) + 1 for line in lines)
    remaining = max_items
    emitted_items = 0
    trimmed = False
    for section in _sections_for_markdown(pack.sections, intent=pack.intent):
        if remaining <= 0 or trimmed:
            break
        section_offset = section_offsets[id(section)].pop(0)
        for position, item in enumerate(section.items[items_per_section:], start=items_per_section):
            index = section_offset + position
            dispositions[index] = ContextRenderDisposition(
                index, item.id, "omitted", "section_limit", "not_emitted"
            )
        section_lines = ["", f"## {section.title}"]
        section_emitted = False
        for position, item in enumerate(section.items[:items_per_section]):
            if remaining <= 0:
                break
            rendered_item = _render_item(
                item,
                pack_project=pack.project,
                max_content_chars=max_content_chars,
                include_related=include_related,
            )
            item_lines = rendered_item.lines
            block = [*section_lines, *item_lines] if not section_emitted else item_lines
            block_chars = sum(len(line) + 1 for line in block)
            if char_budget is not None and emitted_items > 0 and used + block_chars > char_budget:
                trimmed = True
                break
            index = section_offset + position
            item_start = used_bytes + (
                sum(len(line.encode()) + 1 for line in section_lines) if not section_emitted else 0
            )
            content_state = rendered_item.content_state
            spans.extend(
                _item_render_spans(
                    item,
                    rendered_item,
                    index=index,
                    start=item_start,
                )
            )
            dispositions[index] = ContextRenderDisposition(
                index,
                item.id,
                "trimmed" if content_state == "truncated" else "emitted",
                "content_limit" if content_state == "truncated" else "rendered",
                content_state,
            )
            lines.extend(block)
            used += block_chars
            used_bytes += sum(len(line.encode()) + 1 for line in block)
            section_emitted = True
            emitted_items += 1
            remaining -= 1

    if trimmed:
        lines.extend(["", f"_Trimmed to ~{token_budget} tokens; raise --budget for more._"])
    elif pack.usage_hint:
        lines.extend(["", f"_Hint: {pack.usage_hint}_"])

    if trimmed:
        dispositions = {
            index: replace(disposition, reason="budget")
            if disposition.state == "omitted" and disposition.reason == "item_limit"
            else disposition
            for index, disposition in dispositions.items()
        }
    markdown = "\n".join(lines)
    return RenderedContextPack(
        markdown=markdown,
        receipt=ContextRenderReceipt(
            schema_version="sibyl-context-render-v1",
            markdown_sha256=_text_digest(markdown),
            markdown_bytes=len(markdown.encode()),
            selected_items=len(pack.items),
            options=options,
            spans=spans,
            dispositions=list(dispositions.values()),
            request_id=request_id,
        ),
    )


def context_pack_to_markdown(
    pack: ContextPack,
    *,
    max_items: int = 8,
    items_per_section: int = 3,
    max_content_chars: int = 280,
    include_related: bool = True,
    token_budget: int | None = None,
) -> str:
    """Return the unchanged Markdown representation of a context pack."""
    return render_context_pack(
        pack,
        max_items=max_items,
        items_per_section=items_per_section,
        max_content_chars=max_content_chars,
        include_related=include_related,
        token_budget=token_budget,
    ).markdown


def validate_context_render_payload(payload: Mapping[str, Any]) -> list[str]:
    """Validate a retained renderer receipt without fetching mutable source rows.

    Missing receipts belong to older producers. Callers can label them unavailable;
    a present receipt must reconstruct both the exact output and its provenance.
    This verifies artifact consistency, not source authenticity or model consumption.
    """
    receipt = payload.get("render_receipt")
    if receipt is None:
        return []
    if not isinstance(receipt, dict):
        return ["render_receipt must be an object"]
    if receipt.get("schema_version") != "sibyl-context-render-v1":
        return ["unsupported render_receipt schema_version"]
    try:
        pack = TypeAdapter(ContextPack).validate_json(
            json.dumps(dict(payload), allow_nan=False, default=str),
            strict=True,
        )
        parsed = TypeAdapter(ContextRenderReceipt).validate_json(
            json.dumps(receipt, allow_nan=False),
            strict=True,
        )
    except (ValidationError, TypeError, ValueError):
        return ["invalid context render receipt or selected pack"]
    expected = render_context_pack(
        pack,
        **asdict(parsed.options),
        request_id=parsed.request_id,
    )
    failures = []
    if payload.get("markdown") != expected.markdown:
        failures.append("native markdown does not match its selected pack and render options")
    if receipt != asdict(expected.receipt):
        failures.append("render receipt does not match exact emitted spans and dispositions")
    return failures
