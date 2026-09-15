"""Complete evidence hydration and deterministic whole-prefix packing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import UUID

from benchmarks.agent_tasks.screen48.contract import (
    FAMILY_COUNT,
    HEADER,
    POLICY_ROOT,
    canonical,
    digest,
    sha,
    source_geometry,
)
from benchmarks.agent_tasks.screen48.recall.native_evidence import returned_native_evidence

from sibyl_core.memory_pipeline.observations import SourceKind, SourceObservation
from sibyl_core.services.observed_sources import load_authorized_source_snapshot
from sibyl_core.services.source_state_store import RawSourceSnapshot
from sibyl_core.tasks._evidence_json import canonical as evidence_json
from sibyl_core.tasks.episode_evidence import (
    encode_episode_views,
    episode_projection_receipt,
    project_episode,
)

# A construction-qualified family reference is a condensation, never a corpus
# dump: both ceilings are policy, checked here rather than trimmed.
MAX_SUMMARY_CHARS = 16_384
MAX_SUMMARY_TOKENS = 4_096


class MissingPack(ValueError):
    """An observed preparation failure, never a valid empty memory."""


@dataclass(frozen=True)
class Item:
    id: str
    block: str
    evidence: dict

    def receipt(self) -> dict:
        return {"id": self.id, "block_sha256": sha(self.block.encode()), "evidence": self.evidence}


class OriginalCatalog:
    """All 233 retained originals with separately qualified current observations."""

    def __init__(
        self,
        observations: dict[str, SourceObservation],
        authority,
        *,
        policy_root=POLICY_ROOT,
    ):
        self.policy_root = policy_root
        self.rows = {r["source_id"]: r for r in source_geometry(policy_root)}
        self.geometry_digest = digest(self.rows)
        if set(observations) != set(self.rows):
            raise MissingPack("current_original_catalog_incomplete")
        for source_id, observation in observations.items():
            row = self.rows[source_id]
            if (
                str(UUID(source_id)) != source_id
                or observation.source.id != source_id
                or observation.source.kind is not SourceKind.RAW_CAPTURE
                or observation.revision != row["revision"]
                or not observation.durable
                or observation.generation <= 0
                or not observation.incarnation
            ):
                raise MissingPack("invalid_current_source_observation")
        orgs = {o.source.organization_id for o in observations.values()}
        if len(orgs) != 1:
            raise MissingPack("mixed_source_organizations")
        self.organization_id = orgs.pop()
        self.observations = dict(observations)
        self.authority_ceiling = authority.ceiling_metadata()
        self.catalog_sha256 = digest(self.receipt())

    def receipt(self) -> dict:
        return {
            "source_ids": sorted(self.rows),
            "sources": [
                {
                    "id": sid,
                    "source_sha256": self.rows[sid]["source_sha256"],
                    "observation": asdict(self.observations[sid]),
                    "training_family": self.rows[sid]["training_family"],
                }
                for sid in sorted(self.rows)
            ],
            "authority": self.authority_ceiling,
        }

    async def check(self, authority) -> dict[str, RawSourceSnapshot]:
        if (
            digest(self.receipt()) != self.catalog_sha256
            or digest(self.rows) != self.geometry_digest
        ):
            raise MissingPack("retained_catalog_changed")
        source_geometry(self.policy_root)
        if authority.ceiling_metadata() != self.authority_ceiling:
            raise MissingPack("current_authority_changed")
        snapshots = {}
        for source_id, expected in self.observations.items():
            actual = await load_authorized_source_snapshot(
                expected.source, authority, organization_id=self.organization_id
            )
            if (
                not isinstance(actual, RawSourceSnapshot)
                or actual.observation != expected
                or actual.memory.id != source_id
                or actual.memory.organization_id != self.organization_id
                or actual.memory.revision != expected.revision
                or sha(actual.memory.raw_content.encode()) != self.rows[source_id]["source_sha256"]
            ):
                raise MissingPack("original_source_changed")
            snapshots[source_id] = actual
        return snapshots

    def hydrate(self, source_id: str, snapshot: RawSourceSnapshot) -> Item:
        if source_id not in self.rows:
            raise MissingPack("unknown_original_capture_uuid")
        row = self.rows[source_id]
        raw = snapshot.memory.raw_content.encode()
        if snapshot.observation != self.observations[source_id] or sha(raw) != row["source_sha256"]:
            raise MissingPack("original_source_changed")
        tasks = sorted(self.rows, key=lambda sid: self.rows[sid]["training_task"])
        projection = project_episode(source_id, raw, prefix=f"s{tasks.index(source_id):03d}")
        view = encode_episode_views([projection])
        text = evidence_json(view)
        block = f'<source id="{source_id}" sha256="{row["source_sha256"]}">\n{text}\n</source>\n'
        receipt = episode_projection_receipt([(source_id, raw)], [projection], view)
        if sha(block.encode()) != row["block_sha256"] or receipt != row["projection_receipt"]:
            raise MissingPack("complete_projection_changed")
        return Item(
            source_id,
            block,
            {
                "source_sha256": row["source_sha256"],
                "observation": asdict(snapshot.observation),
                "projection": receipt,
            },
        )


def native_key(result) -> str:
    return canonical([result.type, result.id])


def native_item(result, catalog: OriginalCatalog, snapshots: dict) -> Item:
    key = native_key(result)
    evidence = returned_native_evidence(result)
    if result.type == "raw_memory":
        if not result.id.startswith("raw_memory:"):
            raise MissingPack("invalid_typed_raw_capture_identity")
        source_id = result.id.removeprefix("raw_memory:")
        try:
            canonical_uuid = str(UUID(source_id))
        except ValueError as exc:
            raise MissingPack("invalid_typed_raw_capture_identity") from exc
        if canonical_uuid != source_id:
            raise MissingPack("invalid_typed_raw_capture_identity")
        if source_id in catalog.rows:
            hydrated = catalog.hydrate(source_id, snapshots[source_id])
            if result.source_revision != catalog.observations[source_id].revision:
                raise MissingPack("native_original_revision_changed")
            return Item(key, hydrated.block, {"native": evidence, "original": hydrated.evidence})
    # Preserve complete engine-returned passages and public range metadata. They
    # retain their own identity and are never called a complete source projection.
    body = {**evidence, "content": result.content}
    block = "<native>\n" + canonical(body) + "\n</native>\n"
    return Item(key, block, evidence)


def partition(eligible: dict[str, dict], ranked: list[Item], selected_count: int) -> dict:
    ids = [item.id for item in ranked]
    if len(set(ids)) != len(ids) or not set(ids) <= set(eligible):
        raise MissingPack("candidate_catalog_mismatch")
    return {
        "eligible_catalog": eligible,
        "eligible_catalog_sha256": digest(eligible),
        "ranked": [item.receipt() for item in ranked],
        "selected": [item.receipt() for item in ranked[:selected_count]],
        "ranked_budget_omitted": [item.receipt() for item in ranked[selected_count:]],
        "eligible_not_returned": [
            {"id": sid, "evidence": eligible[sid]} for sid in sorted(set(eligible) - set(ids))
        ],
    }


def pack_prefix(
    ranked: list[Item],
    eligible: dict[str, dict],
    *,
    counter,
    prompt: str,
    workspace: dict[str, bytes],
    header: str = HEADER,
    all_required: bool = False,
) -> dict:
    partition(eligible, ranked, 0)
    memory = ""
    counts = counter.request(prompt, memory, workspace)
    if not counts["fits"]:
        raise MissingPack("empty_request_exceeds_context")
    selected = 0
    overflow = None
    for item in ranked:
        candidate = (memory if selected else header) + item.block
        candidate_counts = counter.request(prompt, candidate, workspace)
        if not candidate_counts["fits"]:
            overflow = {"id": item.id, "counts": candidate_counts}
            break
        memory, counts = candidate, candidate_counts
        selected += 1
    if overflow and (not selected or all_required):
        return {
            "status": "missing_pack",
            "reason": "oversized_complete_library" if all_required else "oversized_first_item",
            "memory": None,
            "counts": None,
            "overflow": overflow,
            **partition(eligible, ranked, 0),
        }
    return {
        "status": "prepared",
        "reason": None,
        "memory": memory,
        "counts": counts,
        "overflow": overflow,
        **partition(eligible, ranked, selected),
    }


def summary_items(
    references: dict, catalog: OriginalCatalog, counter, validate_library
) -> list[Item]:
    """Consume a construction-qualified library; never generate or repair it."""
    family_sources: dict[str, set[str]] = {}
    for sid, row in catalog.rows.items():
        family_sources.setdefault(row["training_family"], set()).add(sid)
    if len(references) != FAMILY_COUNT or set(references) != set(family_sources):
        raise MissingPack("incomplete_summary_library")
    # The existing construction validator must return the exact validated input
    # digest, including its source/child receipts, under the frozen observations.
    qualification = validate_library(references, catalog.receipt())
    if qualification != {
        "references_sha256": digest(references),
        "catalog_sha256": catalog.catalog_sha256,
    }:
        raise MissingPack("summary_construction_lineage_unqualified")
    items = []
    for family in sorted(references):
        ref = references[family]
        text = ref["text"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_SUMMARY_CHARS
            or counter.count(text) > MAX_SUMMARY_TOKENS
            or sha(text.encode()) != ref["text_sha256"]
            or set(ref["source_ids"]) != family_sources[family]
            or len(ref["source_ids"]) != len(family_sources[family])
            or not ref.get("construction_receipt_sha256")
            or not ref.get("child_receipts")
        ):
            raise MissingPack("invalid_complete_summary_reference")
        block = f'<summary id="{family}" sha256="{ref["text_sha256"]}">\n{text}\n</summary>\n'
        items.append(Item(family, block, {k: v for k, v in ref.items() if k != "text"}))
    return items
