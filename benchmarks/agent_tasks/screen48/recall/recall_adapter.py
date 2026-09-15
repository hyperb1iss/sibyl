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
    MissingPack,
    OriginalCatalog,
    native_item,
    native_key,
    pack_prefix,
    summary_items,
)

from sibyl_core.retrieval._search_plan import build_context_retrieval_plan
from sibyl_core.retrieval.search import context_search
from sibyl_core.services.content_raw_recall import recall_raw_memory_with_sources

# The native arm reads the product search surface at its own ranked ceiling; the
# raw arm enumerates the complete retained catalog instead of a ranked window.
NATIVE_LIMIT = 50


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
            if checkpoint == 1 and arm in {"raw_retrieval", "strong_summary"}:
                if (
                    prior is None
                    or prior_sha256 != digest(prior)
                    or prior.get("status") != "prepared"
                    or (prior.get("checkpoint"), prior.get("task"), prior.get("arm"))
                    != (0, task, arm)
                    or prior.get("catalog_sha256") != self.catalog.catalog_sha256
                    or prior.get("query_sha256") != base["query_sha256"]
                    or prior.get("prompt_sha256") != base["prompt_sha256"]
                    or prior.get("reader") != base["reader"]
                    or prior.get("before") != before
                ):
                    raise MissingPack("qualified_checkpoint_zero_pack_missing")
                # The caller supplies the separately hash-bound cp0 receipt. The
                # exact source observations and complete request are checked again.
                memory = prior["memory"]
                if arm == "strong_summary":
                    if references is None or digest(references) != prior.get(
                        "summary_references_sha256"
                    ):
                        raise MissingPack("checkpoint_zero_summary_library_changed")
                    validated = summary_items(
                        references, self.catalog, self.counter, self.validate_summary_library
                    )
                    if SUMMARY_HEADER + "".join(item.block for item in validated) != memory:
                        raise MissingPack("checkpoint_zero_summary_memory_changed")
                counts = self.counter.request(prompt, memory, workspace)
                if counts != prior["counts"] or not counts["fits"]:
                    raise MissingPack("checkpoint_zero_pack_changed")
                packed = {
                    key: prior[key]
                    for key in (
                        "status",
                        "reason",
                        "memory",
                        "counts",
                        "overflow",
                        "eligible_catalog",
                        "eligible_catalog_sha256",
                        "ranked",
                        "selected",
                        "ranked_budget_omitted",
                        "eligible_not_returned",
                    )
                }
                packed["reused_checkpoint_zero_sha256"] = digest(prior)
                if arm == "strong_summary":
                    packed["summary_references_sha256"] = prior["summary_references_sha256"]
                diagnostics = prior["diagnostics"]
            elif arm == "no_memory":
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
                response = await context_search(
                    plan=plan, types=None, facet=None, limit=NATIVE_LIMIT, include_content=True
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
