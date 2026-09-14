"""Complete, reconstructible pages of one ordinary controller observation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded
from sibyl_core.tasks.episode_evidence import (
    EpisodeProjection,
    EvidenceCitation,
    encode_episode_views,
    episode_projection_receipt,
    project_episode,
)

VERSION = "sibyl-ordinary-evidence-packets-v1"
QUALIFICATION = (
    "Source-grounded proposal checked against the observed evidence packet, not verified "
    "effectiveness or full-source consistency. Applicability beyond this packet is unestablished. "
    "Other packets can contain qualifying or contradictory observations. "
    "Pages of one capture are one source, not independent experiences."
)
INSTRUCTIONS = (
    "This is one ordered packet of a retained source. The complete source goal and reported "
    "outcome provide context; only the listed events are observed here. Source reports are "
    "not authenticated causal claims. Resolve $ref through values and $literal as literal "
    "object entries. Citations identify exact immutable original UTF-8 byte ranges, not "
    "offsets into this view. Transport and execution audit fields are omitted from assertion "
    "evidence and accounted for by the complete source projection receipt. " + QUALIFICATION
)


def _sha(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _boundaries(projection: EpisodeProjection) -> list[int]:
    events = projection.view["events"]
    return [
        index
        for index, event in enumerate(events)
        if index > 1 and event["kind"] == "model_request"
    ] + [len(events)]


@dataclass(frozen=True)
class OrdinaryEvidencePacket:
    binding_json: str
    payload_json: str
    citations: dict[str, EvidenceCitation]

    @property
    def binding(self) -> dict:
        return json.loads(self.binding_json)

    @property
    def sha256(self) -> str:
        return _sha(json.loads(self.payload_json))

    def permits(self, source_id: str, start: int, end: int) -> bool:
        return any(
            citation.episode_id == source_id and left <= start < end <= right
            for citation in self.citations.values()
            for left, right in citation.ranges
        )


def _view(projection: EpisodeProjection, start: int, end: int) -> EpisodeProjection:
    view = projection.view
    selected = {
        **{key: value for key, value in view.items() if key != "events"},
        "event_range": {"start": start, "end": end, "total": len(view["events"])},
        "events": [
            {"source_event_index": index, **view["events"][index]} for index in range(start, end)
        ],
    }
    ids = {*view["evidence_ids"].values()}
    ids.update(event["evidence_id"] for event in view["events"][start:end])
    return EpisodeProjection(
        selected, {key: projection.citations[key] for key in sorted(ids)}, projection.coverage
    )


def _packet(projection: EpisodeProjection, manifest: dict, index: int) -> OrdinaryEvidencePacket:
    page = manifest["pages"][index]
    selected = _view(projection, page["event_start"], page["event_end"])
    view = encode_episode_views([selected])
    if page["view_sha256"] != _sha(view):
        raise ValueError("ordinary packet view differs from immutable evidence")
    payload = {
        "version": VERSION,
        "manifest_sha256": _sha(manifest),
        "packet_index": index,
        "packet_count": len(manifest["pages"]),
        "source_id": manifest["source_id"],
        "source_sha256": manifest["artifact_sha256"],
        "source_observation": manifest["source_observation"],
        "source_projection": manifest["projection"],
        "packet_view_sha256": page["view_sha256"],
        "evidence_view": view,
        "citations": {
            key: {"episode_id": citation.episode_id, "ranges": citation.ranges}
            for key, citation in selected.citations.items()
        },
        "qualification": QUALIFICATION,
    }
    return OrdinaryEvidencePacket(
        canonical({"manifest": manifest, "index": index}), canonical(payload), selected.citations
    )


def prepare_ordinary_packets(
    source_id: str,
    artifact: bytes,
    *,
    input_chars: Callable[[OrdinaryEvidencePacket], int],
    max_input_chars: int,
    packing_policy: dict | None = None,
    source_observation: dict | None = None,
) -> tuple[OrdinaryEvidencePacket, ...]:
    """Fit every ordered event, including source-wide goal and outcome on each page."""
    if not source_id or type(max_input_chars) is not int or max_input_chars <= 0:
        raise ValueError("ordinary packet source and positive budget required")
    projection = project_episode(source_id, artifact, prefix="s0")
    complete = encode_episode_views([projection])
    manifest = {
        "version": VERSION,
        "source_id": source_id,
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "projection": episode_projection_receipt([(source_id, artifact)], [projection], complete),
        "packing_policy": packing_policy or {},
        "source_observation": source_observation or {},
        "pages": [],
    }
    total = len(projection.view["events"])
    # Keep a model request, its response and resulting tool exchange together.
    # A result on the next page without its call would lose interpretive context.
    boundaries = _boundaries(projection)
    # The maximum possible page count reserves enough framing digits while fitting.
    placeholder = {"event_start": 0, "event_end": 0, "view_sha256": "0" * 64}
    start = 0
    while start < total or not manifest["pages"]:
        accepted = None
        for end in (boundary for boundary in boundaries if boundary > start or total == 0):
            page = {
                "event_start": start,
                "event_end": end,
                "view_sha256": _sha(encode_episode_views([_view(projection, start, end)])),
            }
            provisional = {
                **manifest,
                "pages": [page, *([placeholder] * max(0, total - 1))],
            }
            candidate = _packet(projection, provisional, 0)
            # Reserve the final page-index width as well as the page-count width.
            chars = input_chars(candidate) + max(0, len(str(max(total - 1, 0))) - 1)
            if chars > max_input_chars:
                if accepted is None:
                    raise ConsolidationInputBudgetExceeded(chars, max_input_chars)
                break
            accepted = page
        assert accepted is not None
        manifest["pages"].append(accepted)
        start = accepted["event_end"]
        if total == 0:
            break
    packets = tuple(_packet(projection, manifest, index) for index in range(len(manifest["pages"])))
    for packet in packets:
        chars = input_chars(packet)
        if chars > max_input_chars:
            raise ConsolidationInputBudgetExceeded(chars, max_input_chars)
    return packets


def reconstruct_ordinary_packet(
    source_id: str, artifact: bytes, binding: dict
) -> OrdinaryEvidencePacket:
    """Deny changed sources, unknown pages, and incomplete or overlapping manifests."""
    if set(binding) != {"manifest", "index"} or type(binding["index"]) is not int:
        raise ValueError("invalid ordinary packet binding")
    manifest = binding["manifest"]
    if not isinstance(manifest, dict) or set(manifest) != {
        "version",
        "source_id",
        "artifact_sha256",
        "projection",
        "packing_policy",
        "source_observation",
        "pages",
    }:
        raise ValueError("invalid ordinary packet manifest")
    projection = project_episode(source_id, artifact, prefix="s0")
    receipt = episode_projection_receipt(
        [(source_id, artifact)], [projection], encode_episode_views([projection])
    )
    if (
        manifest["version"] != VERSION
        or not isinstance(manifest["packing_policy"], dict)
        or not isinstance(manifest["source_observation"], dict)
        or manifest["source_id"] != source_id
        or manifest["artifact_sha256"] != hashlib.sha256(artifact).hexdigest()
        or manifest["projection"] != receipt
    ):
        raise ValueError("ordinary packet source or projection changed")
    pages = manifest["pages"]
    if not isinstance(pages, list) or not pages or not 0 <= binding["index"] < len(pages):
        raise ValueError("unknown ordinary evidence packet")
    cursor = 0
    total = len(projection.view["events"])
    for index, page in enumerate(pages):
        if (
            not isinstance(page, dict)
            or set(page) != {"event_start", "event_end", "view_sha256"}
            or type(page["event_start"]) is not int
            or type(page["event_end"]) is not int
            or page["event_start"] != cursor
            or not cursor <= page["event_end"] <= total
            or page["event_end"] not in _boundaries(projection)
            or (total > 0 and page["event_end"] == cursor)
            or (total == 0 and len(pages) != 1)
        ):
            raise ValueError("ordinary packet coverage is incomplete or overlapping")
        _packet(projection, manifest, index)
        cursor = page["event_end"]
    if cursor != total:
        raise ValueError("ordinary packet coverage is incomplete")
    return _packet(projection, manifest, binding["index"])
