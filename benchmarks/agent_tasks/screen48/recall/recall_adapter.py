"""Unarmed preparation adapter over the product recall and observation owners.

Callbacks are existing qualification boundaries, not permission to use fixture
receipts as live authority. No CLI, credential reader or solver entry is provided.
"""
# Every MissingPack below is raised inside prepare()'s try on purpose: the
# handler is what turns an observed failure into a missing_pack receipt, so
# hoisting the raises into helpers would only hide the contract.
# ruff: noqa: TRY301

from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass

from benchmarks.agent_tasks.screen48.contract import (
    ARMS,
    CHECKPOINTS,
    SOURCE_COUNT,
    SUMMARY_HEADER,
    TASKS,
    digest,
    public_task,
    sha,
)
from benchmarks.agent_tasks.screen48.recall.native_evidence import (
    native_evidence,
    returned_native_evidence,
)
from benchmarks.agent_tasks.screen48.recall.whole_items import (
    PACK_FIELDS,
    MissingPack,
    OriginalCatalog,
    native_item,
    native_key,
    pack_prefix,
    summary_items,
)

from sibyl_core.embeddings.providers import configured_embedding_provider
from sibyl_core.retrieval._search_plan import build_context_retrieval_plan
from sibyl_core.retrieval.search import context_search
from sibyl_core.services.content_raw_recall import recall_raw_memory_with_sources

# The native arm reads the product search surface at its own ranked ceiling; the
# raw arm enumerates the complete retained catalog instead of a ranked window.
NATIVE_LIMIT = 50

#: The two arms whose checkpoint-1 memory must carry their checkpoint-0 bytes.
#: Both are re-derived from the live database at checkpoint 1 and compared with
#: the prior, so what the cell carries is always a rendering this run observed.
CP0_BYTE_ARMS = frozenset({"raw_retrieval", "strong_summary"})
RAW_ARM = "raw_retrieval"

#: How a checkpoint-1 reusing cell earned the bytes it carries. `equal_bytes` is
#: the ordinary case: this run's own re-derivation rendered the prior's bytes.
#: `prior_bytes_after_divergence` is the raw control arm's case below.
EQUAL_BYTES = "equal_bytes"
PRIOR_AFTER_DIVERGENCE = "prior_bytes_after_divergence"

#: How many fused rows each side of an accepted raw divergence records inline.
#: Both full rankings are digested and counted beside the excerpt, so the record
#: is complete without carrying two 233-row lists into every receipt.
DIVERGENCE_TOP_K = 10


def before_differences(prior_before, before) -> list[str]:
    """Name the owner-receipt fields the two checkpoints disagree on.

    Evidence for the reviewer, not a gate. Public configuration legitimately
    differs between two runs of one study (an exported provider key in the
    environment of one of them is enough), and the checkpoint-1 contract is
    byte equality of the rendered memory, not an identical runtime.
    """
    prior = prior_before if isinstance(prior_before, dict) else {}
    current = before if isinstance(before, dict) else {}
    return sorted(
        field for field in set(prior) | set(current) if prior.get(field) != current.get(field)
    )


def fused_ranking(pack) -> list:
    """The fused rows a raw pack recorded, or an empty ranking when it recorded none."""
    diagnostics = pack.get("diagnostics") if isinstance(pack, dict) else None
    fused = diagnostics.get("fused") if isinstance(diagnostics, dict) else None
    return list(fused) if isinstance(fused, list) else []


def raw_divergence(prior: dict, fresh: dict, fused: list) -> dict:
    """Record both fused rankings of a raw cell whose re-derivation moved.

    Evidence, not a verdict. A reader gets the two rankings side by side and
    can see what the consolidation cycle did to the BM25 lane's term statistics
    without re-running either checkpoint.
    """
    prior_fused = fused_ranking(prior)
    return {
        "top_k": DIVERGENCE_TOP_K,
        "prior_fused_top": prior_fused[:DIVERGENCE_TOP_K],
        "prior_fused_count": len(prior_fused),
        "prior_fused_sha256": digest(prior_fused),
        "prior_selected_ids": [row["id"] for row in prior.get("selected") or []],
        "prior_memory_sha256": sha(prior["memory"].encode()),
        "fresh_fused_top": fused[:DIVERGENCE_TOP_K],
        "fresh_fused_count": len(fused),
        "fresh_fused_sha256": digest(fused),
        "fresh_selected_ids": [row["id"] for row in fresh["selected"]],
        "fresh_memory_sha256": sha(fresh["memory"].encode()),
    }


@dataclass(frozen=True)
class Reader:
    principal_id: str
    project: str | None
    memory_scope: str
    scope_key: str | None


class RecallAdapter:
    def __init__(
        self,
        *,
        catalog: OriginalCatalog,
        reader: Reader,
        counter,
        resolve_authority,
        verify_owners,
        verify_native_inventory,
        validate_summary_library,
    ):
        self.catalog = catalog
        self.reader = reader
        self.counter = counter
        self.resolve_authority = resolve_authority
        self.verify_owners = verify_owners
        self.verify_native_inventory = verify_native_inventory
        self.validate_summary_library = validate_summary_library

    def prior_bytes(self, prior: dict, *, prompt: str, workspace: dict[str, bytes]) -> dict:
        """The qualified checkpoint-0 pack's own fields, re-counted by this run.

        Nothing is fabricated: every field is the prior's, the memory is its
        exact bytes, and the counts come from this run's counter reading those
        bytes rather than from the prior receipt. A counter that no longer
        reproduces the prior's counts over the prior's bytes has itself moved,
        which is a defect rather than a divergence, so the cell is refused.

        The prior's candidate receipts travel with its bytes, and those name
        their own observation incarnations, so the carry also requires the two
        checkpoints to share one database lifetime. `checkpoints.prior_bindings`
        requires the same thing of the whole prior root; this closes it where
        the receipts are actually carried.
        """
        if any(field not in prior for field in PACK_FIELDS):
            raise MissingPack("qualified_checkpoint_zero_pack_missing")
        if prior.get("catalog_sha256") != self.catalog.catalog_sha256:
            raise MissingPack("checkpoint_zero_lifetime_changed")
        counts = self.counter.request(prompt, prior["memory"], workspace)
        if counts != prior["counts"]:
            raise MissingPack("checkpoint_zero_counts_unreproducible")
        return {**{field: prior[field] for field in PACK_FIELDS}, "counts": counts}

    async def boundary(self):
        self.counter.verify()
        owner_receipt = self.verify_owners()
        if not isinstance(owner_receipt, dict) or not owner_receipt:
            raise MissingPack("source_runtime_unqualified")
        authority = await self.resolve_authority(
            self.catalog.organization_id, self.reader.principal_id
        )
        if authority is None or authority.principal_id != self.reader.principal_id:
            raise MissingPack("current_authority_missing")
        snapshots = await self.catalog.check(authority)
        return authority, snapshots, owner_receipt

    # The arm branches are the study's preparation contract, kept in one place so
    # a reviewer reads the same shape the accepted adapter was qualified in.
    async def prepare(  # noqa: PLR0912, PLR0915
        self,
        *,
        checkpoint: int,
        task: str,
        arm: str,
        native_inventory: dict | None = None,
        references: dict | None = None,
        prior: dict | None = None,
        prior_sha256: str | None = None,
    ) -> dict:
        if (
            type(checkpoint) is not int
            or checkpoint not in CHECKPOINTS
            or arm not in ARMS
            or task not in TASKS
        ):
            raise ValueError("Unknown preparation cell")
        base = {
            "schema": "sibyl-unarmed-whole-item-pack-v3",
            "checkpoint": checkpoint,
            "task": task,
            "arm": arm,
            "catalog": self.catalog.receipt(),
            "catalog_sha256": self.catalog.catalog_sha256,
            "catalog_content_sha256": self.catalog.catalog_content_sha256,
            "solver_calls": 0,
            "reservation_changes": 0,
            "study": "engine_level_diagnostic",
            "reader": asdict(self.reader),
        }
        diagnostics = {}
        try:
            prompt, workspace = public_task(task, self.catalog.policy_root)
            query = " ".join(prompt.strip().split())
            base.update(query_sha256=sha(query.encode()), prompt_sha256=sha(prompt.encode()))
            authority, snapshots, before = await self.boundary()
            base["before"] = before
            reuse = checkpoint == 1 and arm in CP0_BYTE_ARMS
            if reuse:
                if (
                    prior is None
                    or prior_sha256 != digest(prior)
                    or prior.get("status") != "prepared"
                    or not isinstance(prior.get("memory"), str)
                    or (prior.get("checkpoint"), prior.get("task"), prior.get("arm"))
                    != (0, task, arm)
                    or prior.get("catalog_content_sha256") != self.catalog.catalog_content_sha256
                    or prior.get("query_sha256") != base["query_sha256"]
                    or prior.get("prompt_sha256") != base["prompt_sha256"]
                    or prior.get("reader") != base["reader"]
                ):
                    raise MissingPack("qualified_checkpoint_zero_pack_missing")
                # The two owner receipts are recorded as evidence, never required
                # to be equal: the checkpoints run with their own public
                # configuration, and the contract is byte equality of the memory
                # after current authority validation, not an identical
                # environment. The prior's database lifetime stays bound one
                # layer up, by `checkpoints.prior_bindings`.
                base["prior_before"] = prior.get("before")
                base["before_differences"] = before_differences(prior.get("before"), before)
                if arm == "strong_summary" and (
                    references is None
                    or digest(references) != prior.get("summary_references_sha256")
                ):
                    raise MissingPack("checkpoint_zero_summary_library_changed")
            if arm == "no_memory":
                packed = pack_prefix(
                    [], {}, counter=self.counter, prompt=prompt, workspace=workspace
                )
            elif arm == "raw_retrieval":
                if (
                    "capture_ids"
                    not in inspect.signature(recall_raw_memory_with_sources).parameters
                ):
                    raise MissingPack("capture_uuid_filter_owner_not_accepted")
                result = await recall_raw_memory_with_sources(
                    organization_id=self.catalog.organization_id,
                    principal_id=self.reader.principal_id,
                    source_authority=authority,
                    query=query,
                    capture_ids=tuple(sorted(self.catalog.rows)),
                    memory_scope=self.reader.memory_scope,
                    scope_key=self.reader.scope_key,
                    project_id=self.reader.project,
                    limit=SOURCE_COUNT,
                )
                diagnostics = {
                    "source_lanes": [
                        {
                            "source": source.source,
                            "failure": asdict(source.failure) if source.failure else None,
                            "candidate_ids": [m.id for m in source.candidates],
                        }
                        for source in result.sources
                    ],
                    "fused": [
                        {"id": memory.id, "score": memory.score} for memory in result.memories
                    ],
                }
                successes = {s.source for s in result.sources if s.failure is None}
                if result.degraded or not {"raw_fulltext", "raw_vector"} <= successes:
                    raise MissingPack("raw_required_lane_incomplete")
                items = []
                for memory in result.memories:
                    if (
                        memory.id not in snapshots
                        or memory.revision != snapshots[memory.id].memory.revision
                        or sha(memory.raw_content.encode())
                        != self.catalog.rows[memory.id]["source_sha256"]
                    ):
                        raise MissingPack("raw_ranked_source_changed")
                    items.append(self.catalog.hydrate(memory.id, snapshots[memory.id]))
                eligible = {
                    sid: {
                        "source_sha256": row["source_sha256"],
                        "observation": asdict(self.catalog.observations[sid]),
                    }
                    for sid, row in self.catalog.rows.items()
                }
                packed = pack_prefix(
                    items, eligible, counter=self.counter, prompt=prompt, workspace=workspace
                )
            elif arm == "native":
                if native_inventory is None:
                    raise MissingPack("qualified_native_catalog_missing")
                native_before = await self.verify_native_inventory(
                    checkpoint, native_inventory, authority
                )
                if native_before != digest(native_inventory):
                    raise MissingPack("native_catalog_unqualified")
                plan = build_context_retrieval_plan(
                    query=query,
                    organization_id=self.catalog.organization_id,
                    facets=(),
                    facet_types={},
                    principal_id=self.reader.principal_id,
                    project=self.reader.project,
                    accessible_projects=authority.projects,
                    allowed_memory_scope_keys=authority.scope_keys,
                    limit=NATIVE_LIMIT,
                )
                # The product's context tool hands its configured graph embedding
                # provider to the search; without it the vector lane is never
                # attempted and every native pack degrades to "unavailable".
                embedding_provider = configured_embedding_provider()
                if embedding_provider is None:
                    raise MissingPack("native_embedding_provider_unavailable")
                response = await context_search(
                    plan=plan,
                    types=None,
                    facet=None,
                    limit=NATIVE_LIMIT,
                    include_content=True,
                    embedding_provider=embedding_provider,
                )
                diagnostics = {
                    "filters": response.filters,
                    "query": response.query,
                    "total": response.total,
                    "has_more": response.has_more,
                    "ranked": [
                        {"id": native_key(r), "returned": returned_native_evidence(r)}
                        for r in response.results
                    ],
                }
                if (
                    response.query != query
                    or response.total != len(response.results)
                    or len(response.results) > NATIVE_LIMIT
                ):
                    raise MissingPack("native_response_contract_changed")
                for flag in (
                    "fusion_degraded",
                    "candidate_source_degraded",
                    "raw_recall_degraded",
                    "vector_degraded",
                ):
                    if response.filters.get(flag) is not False:
                        raise MissingPack("native_source_or_fusion_degraded")
                if (
                    response.filters.get("vector_requested") is not True
                    or response.filters.get("vector_attempted") is not True
                    or response.filters.get("vector_status") not in {"ok", "empty"}
                ):
                    raise MissingPack("native_vector_lane_incomplete")
                items = []
                for result in response.results:
                    key = native_key(result)
                    if key not in native_inventory or native_inventory[key] != native_evidence(
                        result
                    ):
                        raise MissingPack("native_ranked_item_changed")
                    items.append(native_item(result, self.catalog, snapshots))
                packed = pack_prefix(
                    items,
                    native_inventory,
                    counter=self.counter,
                    prompt=prompt,
                    workspace=workspace,
                )
                if (
                    await self.verify_native_inventory(checkpoint, native_inventory, authority)
                    != native_before
                ):
                    raise MissingPack("native_catalog_changed")
            else:
                items = summary_items(
                    references or {}, self.catalog, self.counter, self.validate_summary_library
                )
                eligible = {item.id: item.evidence for item in items}
                packed = pack_prefix(
                    items,
                    eligible,
                    counter=self.counter,
                    prompt=prompt,
                    workspace=workspace,
                    header=SUMMARY_HEADER,
                    all_required=True,
                )
                packed["summary_references_sha256"] = digest(references)
            if reuse and packed["status"] == "prepared":
                # The frozen contract: checkpoint-1 raw and summary memory bytes
                # must equal their checkpoint-0 bytes after current authority
                # validation. The pack above was derived again from the live
                # database through the same code path checkpoint 0 ran, so this
                # compares two independent renderings rather than vouching for a
                # copy of the prior.
                moved = packed["memory"] != prior["memory"] or packed["counts"] != prior.get(
                    "counts"
                )
                if moved and arm == RAW_ARM:
                    # The raw arm is the study's control and must not see the
                    # treatment. The consolidation cycle wrote its reflection
                    # candidates into the same raw_captures table the 233
                    # originals live in, so the fulltext lane's term statistics
                    # moved and the fused ranking with them, even though the
                    # capture filter still admits only the 233. The contract
                    # asks this cell for its checkpoint-0 bytes, not for an
                    # equal re-derivation, and those bytes have just passed
                    # current authority validation, the content catalog digest
                    # and their own sealed receipt digest. The fresh derivation
                    # cleared every raw lane check above to get here, so a
                    # degraded lane is still a refusal, and both rankings are
                    # recorded for the reviewer.
                    diagnostics["divergence"] = raw_divergence(prior, packed, diagnostics["fused"])
                    packed = self.prior_bytes(prior, prompt=prompt, workspace=workspace)
                    packed["reuse_mode"] = PRIOR_AFTER_DIVERGENCE
                    packed["raw_ranking_diverged"] = True
                elif moved:
                    # The summary library is static material, so a summary
                    # rendering that moved is a real defect in this lane.
                    raise MissingPack("checkpoint_zero_bytes_changed")
                else:
                    packed["reuse_mode"] = EQUAL_BYTES
                    if arm == RAW_ARM:
                        packed["raw_ranking_diverged"] = False
                packed["reused_checkpoint_zero_sha256"] = prior_sha256
            _, _, after = await self.boundary()
            if after != before:
                raise MissingPack("source_runtime_changed_during_preparation")
        except Exception as exc:
            return {
                **base,
                "status": "missing_pack",
                "reason": str(exc) if isinstance(exc, MissingPack) else type(exc).__name__,
                "memory": None,
                "counts": None,
                "diagnostics": diagnostics,
            }
        return {**base, **packed, "diagnostics": diagnostics, "after": after}
