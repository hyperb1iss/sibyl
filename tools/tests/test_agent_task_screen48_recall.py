"""Provider-free controls; fixture authority is never an actual restore receipt.

Ported from the accepted v4 recall-adapter artifact. Two of its cases did not
come across because their evidence is not vendored: the frozen-geometry
projection case needed a signed-episode tarball from the eval host, and the
exact-Qwen-count case needed the tokenizer assets. `test_material_pins` covers
what is checkable in-repo instead, and the counter below stands in for the
tokenizer-backed one the devbox runtime supplies.
"""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from benchmarks.agent_tasks.screen48 import contract as c
from benchmarks.agent_tasks.screen48.recall import native_inventory as ni
from benchmarks.agent_tasks.screen48.recall import recall_adapter as a
from benchmarks.agent_tasks.screen48.recall import whole_items as w
from benchmarks.agent_tasks.screen48.recall.request_count import (
    CONTROLLER_SHA,
    ControllerRequestCounter,
)

from sibyl_core.memory_pipeline.observations import (
    SourceIdentity,
    SourceKind,
    SourceObservation,
)
from sibyl_core.memory_pipeline.retrieval import CandidateSourceResult
from sibyl_core.migrate.source_integrity import ArchiveDatetime
from sibyl_core.services.content_models import RawMemory, RawMemoryRecallResult
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.source_state_store import RawSourceSnapshot
from sibyl_core.tasks._evidence_json import canonical as evidence_json
from sibyl_core.tasks.episode_evidence import (
    encode_episode_views,
    episode_projection_receipt,
    project_episode,
)
from sibyl_core.tools.responses import SearchResponse, SearchResult

TRANSFER_MATERIAL = Path(a.__file__).parents[2] / "transfer_material"
SCHEDULE = Path(c.__file__).parent / "schedule"
GEOMETRY_SHA = "5b3267edbb7daaae0d66b763d58ac37a0e160e8a69c45266f13fc1d6fc9d7a65"
TRANSFER_TASK_COUNT = 24
VENDORED_MATERIAL_FILES = 241
EXPECTED_SOURCE_COUNT = 233
EXPECTED_FAMILY_COUNT = 20
EXPECTED_CELL_COUNT = 48
RANKED_RAW_HITS = 3
BYTES_PER_FIXTURE_TOKEN = 4


class FixtureCounter(ControllerRequestCounter):
    """Deterministic stand-in for the devbox Qwen counter.

    The Qwen tokenizer assets live on the eval host, so these are not the
    study's token counts. Everything else is the real thing: the hash-bound
    controller body, the declared budget, the workspace digests and the fit
    rule all come from `ControllerRequestCounter` unchanged.
    """

    def count(self, text: str) -> int:
        return len(text.encode()) // BYTES_PER_FIXTURE_TOKEN

    def render(self, body: dict) -> str:
        return json.dumps(body, sort_keys=True, ensure_ascii=False)

    def tokenizer_receipt(self) -> dict:
        return {"tokenizer_sha256": "fixture-counter", "tokenizer_config_sha256": "fixture-counter"}


def episode():
    request = {
        "model": "test",
        "messages": [
            {"role": "system", "content": "training input"},
            {"role": "user", "content": "budget"},
        ],
    }
    response = {
        "choices": [{"message": {"role": "assistant", "content": "inspect before changing"}}]
    }

    def wire(value):
        raw = json.dumps(value).encode()
        return {"body_base64": base64.b64encode(raw).decode(), "body_sha256": c.sha(raw)}

    rows = [
        ("start", {"request": {"goal": "training"}, "options": {}, "workspace_initial": {}}),
        ("model_request", {"body": request, **wire(request)}),
        ("model_response", {"raw": response, "status_code": 200, **wire(response)}),
        ("terminal", {"detail": "finished", "exit": 0, "reason": "complete", "usage": {}}),
    ]
    return json.dumps(
        {
            "schema_version": "sibyl-learning-episode-v1",
            "assignment": {},
            "assurance": {},
            "goal": "training",
            "input_memory_pack_sha256": "a" * 64,
            "outcome": {"status": "passed"},
            "sealed_isolation": {},
            "trace": [
                {
                    "schema_version": "sibyl-coding-trace-v1",
                    "attempt_id": "fixture",
                    "request_id": "fixture",
                    "index": i,
                    "kind": kind,
                    "payload": payload,
                }
                for i, (kind, payload) in enumerate(rows)
            ],
        }
    ).encode()


@pytest.fixture(scope="session")
def counter():
    return FixtureCounter()


@pytest.fixture
def setup(monkeypatch, counter):
    # The native arm resolves the product's configured graph embedding provider;
    # tests must not depend on the host having a provider key.
    monkeypatch.setattr(a, "configured_embedding_provider", object)
    raw = episode()
    rows, observations, snapshots = [], {}, {}
    authority = SourceReadAuthority("reader")
    for i in range(c.SOURCE_COUNT):
        sid = str(UUID(int=i + 1))
        observation = SourceObservation(
            SourceIdentity("fixture-org", SourceKind.RAW_CAPTURE, sid),
            1,
            c.sha(raw),
            1,
            True,
            f"fixture-{i}",
        )
        memory = RawMemory(
            id=sid,
            organization_id="fixture-org",
            source_id="upstream-is-not-capture",
            principal_id="reader",
            raw_content=raw.decode(),
        )
        projection = project_episode(sid, raw, prefix=f"s{i:03d}")
        view = encode_episode_views([projection])
        block = f'<source id="{sid}" sha256="{c.sha(raw)}">\n{evidence_json(view)}\n</source>\n'
        rows.append(
            {
                "source_id": sid,
                "source_sha256": c.sha(raw),
                "revision": 1,
                "training_task": f"training-{i:03d}",
                "training_family": f"family-{i % c.FAMILY_COUNT:02d}",
                "block_sha256": c.sha(block.encode()),
                "projection_receipt": episode_projection_receipt([(sid, raw)], [projection], view),
            }
        )
        observations[sid] = observation
        snapshots[sid] = RawSourceSnapshot(memory, observation)
    monkeypatch.setattr(w, "source_geometry", lambda *args: deepcopy(rows))
    catalog = w.OriginalCatalog(observations, authority)
    calls = []

    async def snapshot(source, current_authority, *, organization_id):
        calls.append(source.id)
        assert current_authority == authority
        assert organization_id == "fixture-org"
        return snapshots[source.id]

    monkeypatch.setattr(w, "load_authorized_source_snapshot", snapshot)

    async def resolve(org, principal):
        assert (org, principal) == ("fixture-org", "reader")
        return authority

    async def inventory(cp, items, auth):
        assert auth == authority
        return c.digest(items)

    def library(refs, receipt):
        return {"references_sha256": c.digest(refs), "catalog_sha256": c.digest(receipt)}

    adapter = a.RecallAdapter(
        catalog=catalog,
        reader=a.Reader("reader", None, "private", None),
        counter=counter,
        resolve_authority=resolve,
        verify_owners=lambda: {"fixture_owner": "not_actual_source_acceptance"},
        verify_native_inventory=inventory,
        validate_summary_library=library,
    )
    return SimpleNamespace(
        catalog=catalog,
        authority=authority,
        snapshots=snapshots,
        adapter=adapter,
        calls=calls,
        rows=rows,
        observations=observations,
    )


def refs_for(catalog):
    refs = {}
    for sid, row in catalog.rows.items():
        family = row["training_family"]
        text = f"Complete fixture reference {family}."
        refs.setdefault(
            family,
            {
                "text": text,
                "text_sha256": c.sha(text.encode()),
                "source_ids": [],
                "construction_receipt_sha256": "a" * 64,
                "child_receipts": [{"sha256": "b" * 64}],
            },
        )["source_ids"].append(sid)
    return refs


def healthy_native(results, query):
    return SearchResponse(
        results,
        len(results),
        query,
        {
            "fusion_degraded": False,
            "candidate_source_degraded": False,
            "raw_recall_degraded": False,
            "vector_degraded": False,
            "vector_requested": True,
            "vector_attempted": True,
            "vector_status": "ok",
        },
    )


@pytest.mark.asyncio
async def test_raw_owner_capture_ids_and_cp1_exact_reuse(setup, monkeypatch):
    seen = []
    memories = [snapshot.memory for snapshot in list(setup.snapshots.values())[:3]]

    async def raw(*, capture_ids, **kwargs):
        seen.append({"capture_ids": capture_ids, **kwargs})
        return RawMemoryRecallResult(
            tuple(memories),
            (
                CandidateSourceResult.success("raw_fulltext", memories),
                CandidateSourceResult.success("raw_vector", memories),
            ),
        )

    monkeypatch.setattr(a, "recall_raw_memory_with_sources", raw)
    first = await setup.adapter.prepare(checkpoint=0, task=c.TASKS[0], arm="raw_retrieval")
    assert first["status"] == "prepared", first
    assert len(seen[0]["capture_ids"]) == c.SOURCE_COUNT
    assert set(seen[0]["capture_ids"]) == set(setup.snapshots)
    assert seen[0]["limit"] == c.SOURCE_COUNT
    assert "source_ids" not in seen[0]
    prompt, _ = c.public_task(c.TASKS[0])
    assert seen[0]["query"] == " ".join(prompt.strip().split())
    assert len(first["selected"]) == RANKED_RAW_HITS
    assert len(first["eligible_not_returned"]) == c.SOURCE_COUNT - RANKED_RAW_HITS
    second = await setup.adapter.prepare(
        checkpoint=1,
        task=c.TASKS[0],
        arm="raw_retrieval",
        prior=first,
        prior_sha256=c.digest(first),
    )
    assert second["status"] == "prepared"
    assert second["memory"] == first["memory"]
    assert second["counts"] == first["counts"]
    assert len(seen) == 1
    assert len(setup.calls) == c.SOURCE_COUNT * 4
    changed = deepcopy(first)
    changed["memory"] += "tampered"
    denied = await setup.adapter.prepare(
        checkpoint=1,
        task=c.TASKS[0],
        arm="raw_retrieval",
        prior=changed,
        prior_sha256=c.digest(first),
    )
    assert denied["status"] == "missing_pack"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["missing_vector", "failed_vector", "nonmember", "changed_text"]
)
async def test_raw_lane_and_membership_failures_remain_missing(setup, monkeypatch, failure):
    memory = next(iter(setup.snapshots.values())).memory
    if failure == "nonmember":
        memory = replace(memory, id=str(UUID(int=999)))
    if failure == "changed_text":
        memory = replace(memory, raw_content="different")
    lanes = [CandidateSourceResult.success("raw_fulltext", [memory])]
    if failure == "failed_vector":
        lanes.append(CandidateSourceResult.failed("raw_vector", "TimeoutError"))
    if failure in {"nonmember", "changed_text"}:
        lanes.append(CandidateSourceResult.success("raw_vector", [memory]))

    async def raw(*, capture_ids, **kwargs):
        return RawMemoryRecallResult((memory,), tuple(lanes))

    monkeypatch.setattr(a, "recall_raw_memory_with_sources", raw)
    result = await setup.adapter.prepare(checkpoint=0, task=c.TASKS[0], arm="raw_retrieval")
    assert result["status"] == "missing_pack"
    assert result["memory"] is None


@pytest.mark.asyncio
async def test_source_and_authority_drift_before_and_after(setup, monkeypatch):
    sid = next(iter(setup.snapshots))
    original = setup.snapshots[sid]

    async def native(**kwargs):
        setup.snapshots[sid] = replace(
            original, observation=replace(original.observation, generation=2)
        )
        return healthy_native([], kwargs["plan"].query)

    monkeypatch.setattr(a, "context_search", native)
    result = await setup.adapter.prepare(
        checkpoint=0, task=c.TASKS[0], arm="native", native_inventory={}
    )
    assert result["reason"] == "original_source_changed"
    assert result["memory"] is None
    setup.snapshots[sid] = original

    async def revoked(*args):
        return SourceReadAuthority("reader", projects=frozenset({"changed"}))

    setup.adapter.resolve_authority = revoked
    result = await setup.adapter.prepare(checkpoint=0, task=c.TASKS[0], arm="no_memory")
    assert result["reason"] == "current_authority_changed"


@pytest.mark.asyncio
async def test_native_typed_uuid_hydration_and_public_partial_item(setup, monkeypatch):
    sid = next(iter(setup.snapshots))
    original = setup.snapshots[sid]
    hit = SearchResult(
        f"raw_memory:{sid}",
        "raw_memory",
        "raw",
        original.memory.raw_content,
        0.9,
        source="not-the-uuid",
        source_revision=1,
        result_origin="raw_memory",
    )
    partial = SearchResult(
        "passage:1",
        "pattern",
        "partial",
        "complete returned passage",
        0.8,
        metadata={"source_bindings": {"private": 1}, "passage_index": 1, "passage_total": 3},
    )
    inventory = {w.native_key(r): a.native_evidence(r) for r in (hit, partial)}
    seen = []

    async def native(**kwargs):
        seen.append(kwargs)
        return healthy_native([hit, partial], kwargs["plan"].query)

    monkeypatch.setattr(a, "context_search", native)
    result = await setup.adapter.prepare(
        checkpoint=0, task=c.TASKS[0], arm="native", native_inventory=inventory
    )
    assert result["status"] == "prepared", result
    assert setup.catalog.hydrate(sid, original).block in result["memory"]
    assert "source_bindings" not in result["memory"]
    assert "passage_total" in result["memory"]
    assert seen[0]["types"] is None
    assert seen[0]["facet"] is None
    assert seen[0]["limit"] == a.NATIVE_LIMIT
    assert seen[0]["include_content"] is True
    broken = replace(hit, id="upstream-not-a-uuid")
    with pytest.raises(w.MissingPack, match="typed_raw"):
        w.native_item(broken, setup.catalog, setup.snapshots)


@pytest.mark.asyncio
async def test_native_verified_empty_differs_from_degraded(setup, monkeypatch):
    async def native(**kwargs):
        return healthy_native([], kwargs["plan"].query)

    monkeypatch.setattr(a, "context_search", native)
    result = await setup.adapter.prepare(
        checkpoint=0, task=c.TASKS[0], arm="native", native_inventory={}
    )
    assert result["status"] == "prepared"
    assert result["memory"] == ""

    async def degraded(**kwargs):
        response = healthy_native([], kwargs["plan"].query)
        response.filters["vector_attempted"] = False
        return response

    monkeypatch.setattr(a, "context_search", degraded)
    result = await setup.adapter.prepare(
        checkpoint=0, task=c.TASKS[0], arm="native", native_inventory={}
    )
    assert result["reason"] == "native_vector_lane_incomplete"


@pytest.mark.asyncio
async def test_all_twenty_summary_references_and_complete_source_lineage(setup):
    refs = refs_for(setup.catalog)
    result = await setup.adapter.prepare(
        checkpoint=0, task=c.TASKS[0], arm="strong_summary", references=refs
    )
    assert result["status"] == "prepared", result
    assert len(result["selected"]) == c.FAMILY_COUNT
    assert result["eligible_not_returned"] == []
    assert [r["id"] for r in result["selected"]] == sorted(refs)
    selected_sources = {sid for r in result["selected"] for sid in r["evidence"]["source_ids"]}
    assert len(selected_sources) == c.SOURCE_COUNT
    for mutation in ("family", "source", "text", "child"):
        bad = deepcopy(refs)
        first = bad[sorted(bad)[0]]
        if mutation == "family":
            bad.pop(sorted(bad)[0])
        if mutation == "source":
            first["source_ids"].pop()
        if mutation == "text":
            first["text"] = "changed"
        if mutation == "child":
            first["child_receipts"] = []
        denied = await setup.adapter.prepare(
            checkpoint=0, task=c.TASKS[0], arm="strong_summary", references=bad
        )
        assert denied["status"] == "missing_pack", mutation


def test_whole_prefix_stops_and_retains_partition(counter):
    prompt, workspace = c.public_task(c.TASKS[0])
    ranked = [
        w.Item("first", "first whole item\n", {}),
        w.Item("big", "oversize " * 100_000, {}),
        w.Item("tiny", "tiny\n", {}),
    ]
    eligible = {item.id: {} for item in ranked} | {"not_returned": {"sha256": "a" * 64}}
    result = w.pack_prefix(ranked, eligible, counter=counter, prompt=prompt, workspace=workspace)
    assert result["status"] == "prepared"
    assert [i["id"] for i in result["selected"]] == ["first"]
    assert [i["id"] for i in result["ranked_budget_omitted"]] == ["big", "tiny"]
    assert "tiny" not in result["memory"]
    denied = w.pack_prefix(
        ranked[1:], eligible, counter=counter, prompt=prompt, workspace=workspace
    )
    assert denied["reason"] == "oversized_first_item"
    assert denied["memory"] is None
    denied = w.pack_prefix(
        ranked, eligible, counter=counter, prompt=prompt, workspace=workspace, all_required=True
    )
    assert denied["reason"] == "oversized_complete_library"


def test_exact_actual_controller_body_and_48_denominator(counter):
    prompt, workspace = c.public_task(c.TASKS[1])
    counts = counter.request(prompt, "memory λ\n", workspace)
    body = json.loads(counts["wire_utf8"])
    assert body["seed"] == 0
    assert body["max_tokens"] == c.BUDGET["output_tokens"]
    assert body["model"] == "qwen/qwen3-coder-next"
    assert body["tools"]
    assert body["messages"][-1]["content"] == "memory λ\n"
    assert c.sha(counts["wire_utf8"].encode()) == counts["body_sha256"]
    assert counts["controller_sha256"] == CONTROLLER_SHA
    assert (
        counts["initial_plus_workspace_plus_output"]
        == counts["initial_request_tokens"] + counts["workspace_tokens"] + c.BUDGET["output_tokens"]
    )
    assert [row["path"] for row in counts["workspace"]] == sorted(workspace)
    assert all(row["sha256"] == c.sha(workspace[row["path"]]) for row in counts["workspace"])
    grid = c.preparation_grid({})
    assert len(grid["cells"]) == grid["denominator"] == c.CELL_COUNT == EXPECTED_CELL_COUNT
    assert all(row["status"] == "missing_pack" for row in grid["cells"])
    assert grid["attempt_ids"] is None
    assert grid["execution_order"] is None
    assert not grid["complete"]
    with pytest.raises(ValueError, match="Unexpected preparation cell"):
        c.preparation_grid({(0, "future-task", "native"): {}})


@pytest.mark.asyncio
async def test_actual_observation_owner_denies_revoked_source(setup, monkeypatch):
    from contextlib import asynccontextmanager  # noqa: PLC0415

    from sibyl_core.services import content_client, observed_sources  # noqa: PLC0415

    for sid, snapshot in list(setup.snapshots.items()):
        setup.snapshots[sid] = replace(
            snapshot, memory=replace(snapshot.memory, observed_revision=1)
        )

    @asynccontextmanager
    async def client():
        yield SimpleNamespace(execute_query=None)

    async def read(source, **kwargs):
        return setup.snapshots[source.id]

    monkeypatch.setattr(content_client, "surreal_content_client", client)
    monkeypatch.setattr(observed_sources, "load_source_snapshot", read)
    monkeypatch.setattr(
        w, "load_authorized_source_snapshot", observed_sources.load_authorized_source_snapshot
    )
    result = await setup.catalog.check(setup.authority)
    assert len(result) == c.SOURCE_COUNT
    sid = next(iter(setup.snapshots))
    snapshot = setup.snapshots[sid]
    setup.snapshots[sid] = replace(
        snapshot, memory=replace(snapshot.memory, principal_id="different-owner")
    )
    with pytest.raises(Exception) as failure:  # noqa: PT011
        await setup.catalog.check(setup.authority)
    assert type(failure.value).__name__ == "SourceUnavailableError"


@pytest.mark.asyncio
async def test_public_input_drift_remains_in_missing_grid(setup, monkeypatch):
    def changed(*args):
        raise ValueError("bound public prompt changed")

    monkeypatch.setattr(a, "public_task", changed)
    pack = await setup.adapter.prepare(checkpoint=0, task=c.TASKS[0], arm="native")
    assert pack["status"] == "missing_pack"
    assert pack["memory"] is None
    grid = c.preparation_grid({(0, c.TASKS[0], "native"): pack})
    assert len(grid["cells"]) == c.CELL_COUNT
    assert all(r["status"] == "missing_pack" for r in grid["cells"])


@pytest.mark.asyncio
async def test_summary_cp1_revalidates_child_receipts_without_rebuilding(setup):
    refs = refs_for(setup.catalog)
    prior = await setup.adapter.prepare(
        checkpoint=0, task=c.TASKS[0], arm="strong_summary", references=refs
    )
    second = await setup.adapter.prepare(
        checkpoint=1,
        task=c.TASKS[0],
        arm="strong_summary",
        references=refs,
        prior=prior,
        prior_sha256=c.digest(prior),
    )
    assert second["status"] == "prepared"
    assert second["memory"] == prior["memory"]
    assert second["counts"] == prior["counts"]

    def changed_child(*args):
        raise w.MissingPack("retained_summary_child_changed")

    setup.adapter.validate_summary_library = changed_child
    denied = await setup.adapter.prepare(
        checkpoint=1,
        task=c.TASKS[0],
        arm="strong_summary",
        references=refs,
        prior=prior,
        prior_sha256=c.digest(prior),
    )
    assert denied["reason"] == "retained_summary_child_changed"
    assert denied["memory"] is None


def assert_material_freeze_pins_every_vendored_byte():
    """The transfer material is byte-pinned by its own frozen selection receipt.

    ``material-freeze.json`` is the receipt the qualification lane sealed before
    any solver ran, copied verbatim from the retained artifact, and it carries a
    sha256 for every authored file. That is what makes ruff.toml's refusal to
    touch this tree checkable rather than a claim. Two files are the harness's
    own and carry no digest: the freeze cannot pin itself, and ruff.toml
    postdates it.
    """
    freeze = json.loads((TRANSFER_MATERIAL / "material-freeze.json").read_bytes())
    assert freeze["selection_frozen_before_solver_runs"] is True
    assert freeze["solver_runs"] == 0
    digests = freeze["files"]
    vendored = [
        path
        for path in sorted(TRANSFER_MATERIAL.rglob("*"))
        if path.is_file()
        and not any(
            part.startswith(".") or part == "__pycache__"
            for part in path.relative_to(TRANSFER_MATERIAL).parts
        )
    ]
    unpinned = {"material-freeze.json", "ruff.toml"}
    pinned = 0
    for path in vendored:
        relative = path.relative_to(TRANSFER_MATERIAL).as_posix()
        if relative in unpinned:
            continue
        assert relative in digests, relative
        assert c.sha(path.read_bytes()) == digests[relative], relative
        pinned += 1
    assert pinned == len(vendored) - len(unpinned) == VENDORED_MATERIAL_FILES
    # Each of the 24 tasks carries its prompt, its oracle and both file trees.
    catalog = json.loads((TRANSFER_MATERIAL / "material-catalog.json").read_bytes())
    for task in sorted(entry["id"] for entry in catalog["tasks"]):
        assert f"tasks/{task}/prompt.md" in digests, task
        assert f"tasks/{task}/oracle.json" in digests, task
        for tree in ("workspace", "reference"):
            present = {key for key in digests if key.startswith(f"tasks/{task}/{tree}/")}
            assert len(present) == len(c.WORKSPACE_FILES), (task, tree)


def test_material_pins():
    """Every vendored binding the preparation lane resolves without the eval host."""
    # The source geometry is the study denominator: 233 captures, 20 families.
    rows = c.source_geometry()
    assert len(rows) == c.SOURCE_COUNT == EXPECTED_SOURCE_COUNT
    assert len({r["source_id"] for r in rows}) == c.SOURCE_COUNT
    families = {r["training_family"] for r in rows}
    assert len(families) == c.FAMILY_COUNT == EXPECTED_FAMILY_COUNT

    # The pinned policy bytes are the ones that shipped.
    assert c.sha((c.POLICY_ROOT / "policy.json").read_bytes()) == c.POLICY_SHA
    assert c.sha((c.POLICY_ROOT / "bindings.json").read_bytes()) == c.POLICY_BINDINGS_SHA
    geometry_path = c.POLICY_ROOT / "measurement-v2/source-geometry.json"
    assert c.sha(geometry_path.read_bytes()) == c.GEOMETRY_SHA

    # The measurement receipt and public_task() agree on every task's inputs,
    # which is two independently vendored files checking each other.
    geometry = json.loads(c.bound(c.POLICY_ROOT / "measurement-v2/geometry.json", GEOMETRY_SHA))
    assert geometry["source_geometry_sha256"] == c.GEOMETRY_SHA
    assert geometry["source_count"] == c.SOURCE_COUNT
    assert geometry["training_families"] == c.FAMILY_COUNT
    assert [row["task_id"] for row in geometry["tasks"]] == list(c.TASKS)
    for row in geometry["tasks"]:
        prompt, workspace = c.public_task(row["task_id"])
        assert c.sha(prompt.encode()) == row["prompt_sha256"]
        assert sorted(workspace) == sorted(c.WORKSPACE_FILES)
        for binding in row["workspace"]:
            assert c.sha(workspace[binding["path"]]) == binding["sha256"]

    # The accepted strong_summary library covers every family and every source.
    library = json.loads(c.SUMMARY_LIBRARY.read_bytes())
    assert set(library) == families
    assert len(library) == c.FAMILY_COUNT
    assert len({sid for ref in library.values() for sid in ref["source_ids"]}) == c.SOURCE_COUNT
    for family, ref in library.items():
        assert c.sha(ref["text"].encode()) == ref["text_sha256"], family
        assert ref["construction_receipt_sha256"], family
        assert ref["child_receipts"], family
    provenance = json.loads((c.MATERIAL_ROOT / "summary-library.provenance.json").read_bytes())
    assert provenance["summary_library_sha256"] == c.sha(c.SUMMARY_LIBRARY.read_bytes())
    assert provenance["references"] == c.FAMILY_COUNT
    assert provenance["source_ids"] == c.SOURCE_COUNT

    # The frozen 48-cell schedule still describes the contract's grid.
    schedule = json.loads((SCHEDULE / "schedule.json").read_bytes())
    assert schedule["denominator"] == c.CELL_COUNT == len(schedule["cells"])
    assert schedule["denominator"] == EXPECTED_CELL_COUNT
    assert schedule["arms"] == list(c.ARMS)
    assert schedule["checkpoints"] == list(c.CHECKPOINTS)
    assert schedule["tasks_policy_order"] == list(c.TASKS)
    assert schedule["source_admissions"] == c.SOURCE_COUNT
    assert schedule["training_families"] == c.FAMILY_COUNT
    assert {(cell["checkpoint"], cell["task"], cell["arm"]) for cell in schedule["cells"]} == {
        (cp, task, arm) for cp in c.CHECKPOINTS for task in c.TASKS for arm in c.ARMS
    }
    assert (SCHEDULE / "proposal.md").read_bytes()
    assert json.loads((SCHEDULE / "budget-proposal.json").read_bytes())

    # The schedule's per-task bindings tie all three vendored trees together:
    # the policy prompt and workspace bytes on one side, the transfer material's
    # oracle on the other.
    bindings = json.loads((SCHEDULE / "task-bindings.json").read_bytes())
    assert {binding["task_id"] for binding in bindings} == set(c.TASKS)
    for binding in bindings:
        prompt, workspace = c.public_task(binding["task_id"])
        assert c.sha(prompt.encode()) == binding["public_prompt"]["sha256"]
        for file in binding["workspace"]:
            assert c.sha(workspace[file["destination"]]) == file["sha256"]
        oracle = TRANSFER_MATERIAL / "tasks" / binding["task_id"] / "oracle.json"
        frozen = binding["private_oracle_metadata_only"]["sha256_from_retained_material_freeze"]
        assert c.sha(oracle.read_bytes()) == frozen, binding["task_id"]

    assert_material_freeze_pins_every_vendored_byte()


def test_transfer_task_material_is_complete():
    catalog = json.loads((TRANSFER_MATERIAL / "material-catalog.json").read_bytes())
    directories = sorted(p.name for p in (TRANSFER_MATERIAL / "tasks").iterdir() if p.is_dir())
    assert len(directories) == TRANSFER_TASK_COUNT
    assert sorted(task["id"] for task in catalog["tasks"]) == directories
    # The six screen tasks are all carried by the transfer material.
    assert set(c.TASKS) <= set(directories)
    for task in catalog["tasks"]:
        root = TRANSFER_MATERIAL / "tasks" / task["id"]
        assert (root / "prompt.md").read_bytes(), task["id"]
        assert json.loads((root / "oracle.json").read_bytes()), task["id"]
        workspace = sorted(p.name for p in (root / "workspace").iterdir())
        assert workspace == sorted(task["workspace_files"]) == sorted(c.WORKSPACE_FILES)
        reference = sorted(p.name for p in (root / "reference").iterdir())
        assert reference == sorted(c.WORKSPACE_FILES), task["id"]


def test_counter_protocol_is_satisfied_without_tokenizer_assets(counter):
    from benchmarks.agent_tasks.screen48.recall.request_count import RequestCounter  # noqa: PLC0415

    assert isinstance(counter, RequestCounter)
    counter.verify()
    prompt, workspace = c.public_task(c.TASKS[0])
    counts = counter.request(prompt, "", workspace)
    assert counts["fits"] is True
    assert counts["memory_tokens"] == 0
    assert counts["provider_usage"] is None
    assert counts["future_history_fit_guaranteed"] is False
    assert counts["budget"] == c.BUDGET
    assert asdict(a.Reader("p", None, "private", None)) == {
        "principal_id": "p",
        "project": None,
        "memory_scope": "private",
        "scope_key": None,
    }


@pytest.mark.asyncio
async def test_native_search_receives_the_configured_graph_embedding_provider(setup, monkeypatch):
    seen: list[dict] = []
    sentinel = object()

    async def native(**kwargs):
        seen.append(kwargs)
        return healthy_native([], kwargs["plan"].query)

    monkeypatch.setattr(a, "context_search", native)
    monkeypatch.setattr(a, "configured_embedding_provider", lambda: sentinel)
    result = await setup.adapter.prepare(
        checkpoint=0, task=c.TASKS[0], arm="native", native_inventory={}
    )
    assert result["status"] == "prepared"
    assert seen[0]["embedding_provider"] is sentinel


@pytest.mark.asyncio
async def test_native_arm_names_a_missing_graph_embedding_provider(setup, monkeypatch):
    calls: list[dict] = []

    async def native(**kwargs):
        calls.append(kwargs)
        return healthy_native([], kwargs["plan"].query)

    monkeypatch.setattr(a, "context_search", native)
    monkeypatch.setattr(a, "configured_embedding_provider", lambda: None)
    result = await setup.adapter.prepare(
        checkpoint=0, task=c.TASKS[0], arm="native", native_inventory={}
    )
    assert result["status"] == "missing_pack"
    assert result["reason"] == "native_embedding_provider_unavailable"
    assert calls == []


# ---------------------------------------------------------------------------
# The graph branch of the native inventory: one promoted entity read twice.
# ---------------------------------------------------------------------------

GRAPH_UUID = "procedure_v3_fixture"
GRAPH_RECORD_KEY = f"entity:{GRAPH_UUID}"
GRAPH_CREATED = "2026-09-17T19:18:52.746008Z"
GRAPH_UPDATED = "2026-09-17T19:19:05.265003Z"
GRAPH_REVISION = 3


def archive_graph_row(**overrides):
    """The row shape `read_source_archive_snapshot` returns: no `id`, aliased key.

    Its datetimes arrive as `ArchiveDatetime`, carrying the native text the SDK
    truncates, which is the second asymmetry between the two reads.
    """
    row = {
        "uuid": GRAPH_UUID,
        "group_id": "fixture-org",
        "entity_type": "procedure",
        "name": "Repair the fixture CLI",
        "summary": "fixture summary",
        "content": "inspect before changing",
        "revision": GRAPH_REVISION,
        "derivation_required": True,
        "created_at": ArchiveDatetime.parse(GRAPH_CREATED),
        "updated_at": ArchiveDatetime.parse(GRAPH_UPDATED),
        "attributes": {
            "entity_type": "procedure",
            "memory_scope": "private",
            "principal_id": "reader",
            "updated_at": ArchiveDatetime.parse(GRAPH_UPDATED),
        },
        "archive_record_key": GRAPH_RECORD_KEY,
    }
    return row | overrides


def scoped_graph_row(**overrides):
    """The row shape the scoped graph read returns: `id` renamed to `record_id`.

    `normalize_graph_records` performs that rename, and entity decoding copies
    the value into metadata, which is the field the two reads disagreed on.
    """
    row = {key: value for key, value in archive_graph_row().items() if key != "archive_record_key"}
    row["record_id"] = GRAPH_RECORD_KEY
    row["created_at"] = datetime(2026, 9, 17, 19, 18, 52, 746008, tzinfo=UTC)
    row["updated_at"] = datetime(2026, 9, 17, 19, 19, 5, 265003, tzinfo=UTC)
    row["attributes"] = dict(row["attributes"]) | {"updated_at": row["updated_at"]}
    return row | overrides


def graph_state_row(**overrides):
    row = {
        "organization_id": "fixture-org",
        "source_kind": "graph_entity",
        "source_id": GRAPH_UUID,
        "deleted": False,
        "revision": GRAPH_REVISION,
        "generation": 1,
        "incarnation": "fixture-incarnation",
    }
    return row | overrides


def archive_snapshot(rows, states, *, auxiliary=True, fingerprint="a"):
    snapshot = {
        "source_rows": rows,
        "source_states": states,
        "derivations": [],
        "fingerprint": fingerprint * 64,
    }
    if auxiliary:
        snapshot["graph_auxiliary"] = {"episode": [], "relates_to": [], "mentions": []}
    return snapshot


@pytest.fixture
def inventory_graph(monkeypatch):
    """Wire `enumerate_current`'s owners around one promoted graph entity."""
    state = SimpleNamespace(
        archive=archive_graph_row(),
        scoped=scoped_graph_row(),
        source_state=graph_state_row(),
    )

    async def snapshots(execute_query, *, kind, organizations, include_graph_auxiliary=False):
        assert organizations == ["fixture-org"]
        if kind is SourceKind.GRAPH_ENTITY:
            return archive_snapshot(
                [deepcopy(state.archive)], [deepcopy(state.source_state)], fingerprint="a"
            )
        return archive_snapshot([], [], auxiliary=False, fingerprint="b")

    async def visible(org, ids, *, runtime=None):
        assert (org, ids) == ("fixture-org", [GRAPH_UUID])
        return {GRAPH_UUID: entity_from_surreal_row(state.scoped)}

    async def no_raw(**kwargs):
        return []

    async def gate(*, client, group_id, source_lists, plan):
        return [(signal, list(candidates)) for signal, candidates in source_lists], {
            "fixture_gate": True
        }

    class Content:
        async def __aenter__(self):
            return SimpleNamespace(execute_query=None)

        async def __aexit__(self, *exc):
            return False

    async def runtime(org, *, ensure_schema=True):
        return SimpleNamespace(client=SimpleNamespace(execute_query=None, close=None))

    monkeypatch.setattr(ni, "get_surreal_graph_runtime", runtime)
    monkeypatch.setattr(ni, "read_source_archive_snapshot", snapshots)
    monkeypatch.setattr(ni, "surreal_content_client", Content)
    monkeypatch.setattr(ni, "available_graph_entities", visible)
    monkeypatch.setattr(ni, "list_raw_memories_for_scope", no_raw)
    monkeypatch.setattr(ni, "_apply_supersession_gate", gate)
    monkeypatch.setattr(ni, "_candidate_allowed", lambda *args, **kwargs: True)
    # The observation owner is exercised by its own tests; here it stands in for
    # the durable ledger so the assertions stay on the row-agreement check.
    monkeypatch.setattr(
        ni, "observe_graph_snapshot", lambda snapshot, source, authority: snapshot.observation
    )
    return state


async def enumerate_graph():
    return await ni.enumerate_current(
        organization_id="fixture-org",
        authority=SourceReadAuthority("reader"),
        reader=a.Reader("reader", None, "private", None),
    )


@pytest.mark.asyncio
async def test_inventory_accepts_the_two_reads_of_one_unchanged_graph_row(inventory_graph):
    items, receipt = await enumerate_graph()
    assert receipt["source_counts"]["entity"] == 1
    assert receipt["excluded"] == {}
    assert len(items) == 1
    provenance = next(iter(receipt["provenance"].values()))
    assert provenance["kind"] == "graph_entity"
    assert provenance["observation"]["generation"] == 1
    assert provenance["observation"]["revision"] == GRAPH_REVISION
    # The scoped read's projected record key is bound to the archive row's own,
    # rather than being compared as if it were stored row content.
    scoped = entity_from_surreal_row(inventory_graph.scoped)
    assert scoped.metadata["record_id"] == GRAPH_RECORD_KEY
    assert "record_id" not in entity_from_surreal_row(inventory_graph.archive).metadata


@pytest.mark.asyncio
async def test_inventory_still_refuses_a_changed_graph_row(inventory_graph):
    inventory_graph.scoped = scoped_graph_row(content="inspect after changing")
    with pytest.raises(w.MissingPack, match="available_graph_row_changed"):
        await enumerate_graph()


@pytest.mark.asyncio
async def test_inventory_still_refuses_a_changed_graph_row_name(inventory_graph):
    inventory_graph.scoped = scoped_graph_row(name="Repair another CLI")
    with pytest.raises(w.MissingPack, match="available_graph_row_changed"):
        await enumerate_graph()


@pytest.mark.asyncio
async def test_inventory_refuses_a_graph_row_read_under_another_record_key(inventory_graph):
    inventory_graph.scoped = scoped_graph_row(record_id="entity:another_physical_row")
    with pytest.raises(w.MissingPack, match="available_graph_record_key_changed"):
        await enumerate_graph()


@pytest.mark.asyncio
async def test_inventory_compares_a_stored_record_key_between_both_reads(inventory_graph):
    stored = {"record_id": "entity:stored_in_attributes"}
    inventory_graph.archive = archive_graph_row(
        attributes=archive_graph_row()["attributes"] | stored
    )
    inventory_graph.scoped = scoped_graph_row(
        attributes=scoped_graph_row()["attributes"] | stored, record_id=GRAPH_RECORD_KEY
    )
    items, _ = await enumerate_graph()
    assert len(items) == 1
    inventory_graph.scoped = scoped_graph_row(
        attributes=scoped_graph_row()["attributes"] | {"record_id": "entity:other"},
        record_id=GRAPH_RECORD_KEY,
    )
    with pytest.raises(w.MissingPack, match="available_graph_record_key_changed"):
        await enumerate_graph()


@pytest.mark.asyncio
async def test_inventory_refuses_a_graph_row_whose_observed_revision_moved(inventory_graph):
    inventory_graph.scoped = scoped_graph_row(revision=GRAPH_REVISION + 1)
    with pytest.raises(w.MissingPack, match="available_graph_row_changed"):
        await enumerate_graph()


@pytest.mark.asyncio
async def test_inventory_refuses_a_graph_row_whose_derivation_flag_moved(inventory_graph):
    """The model excludes this flag from its dump; the inventory binds it anyway."""
    inventory_graph.scoped = scoped_graph_row(derivation_required=False)
    scoped = entity_from_surreal_row(inventory_graph.scoped).model_dump(mode="json")
    archived = entity_from_surreal_row(inventory_graph.archive).model_dump(mode="json")
    scoped["metadata"].pop("record_id")
    assert scoped == archived
    with pytest.raises(w.MissingPack, match="available_graph_row_changed"):
        await enumerate_graph()
