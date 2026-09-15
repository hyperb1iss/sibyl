"""Unit cover for screen48 checkpoint pack preparation.

Every product owner is faked, in the style of the recall tests: no database, no
broker, no tokenizer assets and no signed cohort archive. What is under test is
this module's own policy, which is the part that decides what lands on disk and
what the launcher is later allowed to execute.
"""

# Cell counts and exit codes are the assertions here.
# ruff: noqa: PLR2004

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from benchmarks.agent_tasks.screen48 import checkpoints, contract, cycle, materialize

#: The frozen schedule's catalog digest, pinned here so a silent material or
#: database swap cannot quietly agree with itself.
SCHEDULE_CATALOG_SHA256 = "d2d406d004e71ced67ae7dd8af52d2b2bbd0ac6330cf8616b6c3d99f5567d487"

MATERIALIZE_KEYS = frozenset(
    {"status", "reason", "checkpoint", "task", "arm", "memory", "counts", "catalog_sha256"}
)


class FakeCatalog:
    """The shape ``prepare_checkpoint`` reads off a qualified original catalog."""

    def __init__(self, catalog_sha256: str, source_count: int = contract.SOURCE_COUNT) -> None:
        self.catalog_sha256 = catalog_sha256
        self.organization_id = contract.ORGANIZATION_ID
        self.rows = {str(UUID(int=i + 1)): {"revision": 1} for i in range(source_count)}

    def receipt(self) -> dict[str, Any]:
        return {"source_ids": sorted(self.rows)}


class FakeCounter:
    """Stands in for the devbox Qwen counter; only the receipt is read here."""

    def __init__(self, assets: Path) -> None:
        self.assets = assets
        self.verified = 0

    def verify(self) -> None:
        self.verified += 1

    def tokenizer_receipt(self) -> dict[str, str]:
        return {"tokenizer_sha256": "fixture", "tokenizer_config_sha256": "fixture"}


class FakeNatives:
    """A bound checkpoint inventory that answers from the fixture, not the graph."""

    def __init__(self, state: Any, **kwargs: Any) -> None:
        self.state = state
        self.bindings = kwargs
        self.produced: list[int] = []

    async def produce(self, checkpoint: int) -> tuple[dict, dict]:
        self.produced.append(checkpoint)
        return self.state.items, self.state.inventory

    async def verify(self, checkpoint: int, items: dict, authority: Any) -> str:
        return contract.digest(items)


class FakeAdapter:
    """Return canned preparation receipts and record exactly what was asked for."""

    def __init__(self, state: Any, **kwargs: Any) -> None:
        self.state = state
        self.bindings = kwargs

    async def prepare(
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
        self.state.calls.append(
            {
                "checkpoint": checkpoint,
                "task": task,
                "arm": arm,
                "native_inventory": native_inventory,
                "references": references,
                "prior": prior,
                "prior_sha256": prior_sha256,
            }
        )
        base = {
            "schema": "sibyl-unarmed-whole-item-pack-v3",
            "checkpoint": checkpoint,
            "task": task,
            "arm": arm,
            "catalog_sha256": self.state.catalog_sha256,
            "reader": {"principal_id": contract.PRINCIPAL_ID},
        }
        if (task, arm) in self.state.missing:
            return {
                **base,
                "status": "missing_pack",
                "reason": "raw_ranked_source_changed",
                "memory": None,
                "counts": None,
            }
        if checkpoint == 1 and arm in checkpoints.CP1_PRIOR_ARMS and prior is None:
            return {
                **base,
                "status": "missing_pack",
                "reason": "qualified_checkpoint_zero_pack_missing",
                "memory": None,
                "counts": None,
            }
        memory = "" if arm == checkpoints.NO_MEMORY_ARM else f"pack cp{checkpoint} {task} {arm} λ\n"
        return {
            **base,
            "status": "prepared",
            "reason": None,
            "memory": memory,
            "counts": {"memory_tokens": len(memory), "fits": True},
        }


def raw_only_inventory(count: int = 2) -> tuple[dict, dict]:
    """A checkpoint-0 shaped inventory: retained raw originals and nothing else."""
    items = {f'["raw_memory","raw_memory:{i}"]': {"content_sha256": "a" * 64} for i in range(count)}
    return items, {
        "schema": "sibyl-authorized-native-inventory-v1",
        "authorized_count": count,
        "source_counts": {"entity": 0, "episode": 0, "relationship": 0, "raw_capture": count},
        "provenance": {key: {"kind": "raw_capture"} for key in items},
        "excluded": {},
    }


def consolidated_inventory() -> tuple[dict, dict]:
    """A checkpoint-1 shaped inventory: the cycle's publications beside the originals."""
    items, receipt = raw_only_inventory()
    items['["node","entity-1"]'] = {"content_sha256": "b" * 64}
    items['["relationship","edge-1"]'] = {"content_sha256": "c" * 64}
    receipt["provenance"]['["node","entity-1"]'] = {"kind": "graph_entity"}
    receipt["provenance"]['["relationship","edge-1"]'] = {"kind": "relationship"}
    receipt["source_counts"].update(entity=1, relationship=1)
    receipt["authorized_count"] = len(items)
    return items, receipt


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> Any:
    items, inventory = raw_only_inventory()
    fixture = SimpleNamespace(
        catalog_sha256=SCHEDULE_CATALOG_SHA256,
        source_count=contract.SOURCE_COUNT,
        items=items,
        inventory=inventory,
        missing=set(),
        calls=[],
        counters=[],
        natives=[],
        adapters=[],
        qualified=[],
    )

    async def noop() -> None:
        return None

    monkeypatch.setattr(cycle, "bootstrap_runtime", noop)
    monkeypatch.setattr(cycle, "shutdown_runtime", noop)

    async def qualify(*, group_id: str, principal_id: str) -> tuple[Any, dict, Any, Any]:
        fixture.qualified.append((group_id, principal_id))
        catalog = FakeCatalog(fixture.catalog_sha256, fixture.source_count)
        return catalog, {"status": "qualified"}, "authority", lambda: {"owner": "fixture"}

    def counter(assets: Path) -> FakeCounter:
        made = FakeCounter(assets)
        fixture.counters.append(made)
        return made

    def natives(**kwargs: Any) -> FakeNatives:
        made = FakeNatives(fixture, **kwargs)
        fixture.natives.append(made)
        return made

    def adapter(**kwargs: Any) -> FakeAdapter:
        made = FakeAdapter(fixture, **kwargs)
        fixture.adapters.append(made)
        return made

    monkeypatch.setattr(checkpoints, "qualify_catalog", qualify)
    monkeypatch.setattr(checkpoints, "QwenRequestCounter", counter)
    monkeypatch.setattr(checkpoints, "NativeCheckpoints", natives)
    monkeypatch.setattr(checkpoints, "RecallAdapter", adapter)
    return fixture


def run(checkpoint: int, output: Path, prior_root: Path | None = None) -> int:
    argv = ["--checkpoint", str(checkpoint), "--output", str(output), "--tokenizer-assets", "/t"]
    if prior_root is not None:
        argv += ["--prior-root", str(prior_root)]
    return checkpoints.main(argv)


def receipt_of(output: Path) -> dict[str, Any]:
    return json.loads((output / checkpoints.RECEIPT_NAME).read_text(encoding="utf-8"))


def pack_of(output: Path, checkpoint: int, task: str, arm: str) -> dict[str, Any]:
    path = output / "packs" / f"cp{checkpoint}" / task / f"{arm}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The checkpoint-0 raw-original-only gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_zero_refuses_a_derived_native_inventory(
    tmp_path: Path, state: Any
) -> None:
    state.items, state.inventory = consolidated_inventory()
    output = tmp_path / "cp0"

    with pytest.raises(checkpoints.CheckpointError, match="raw originals only"):
        await checkpoints.prepare_checkpoint(
            0, output=output, tokenizer_assets=tmp_path, prior_root=None
        )

    sealed = receipt_of(output)
    assert sealed["status"] == checkpoints.STATUS_DERIVED_AT_ZERO
    assert sealed["native_inventory"]["derived_source_counts"] == {
        "entity": 1,
        "episode": 0,
        "relationship": 1,
    }
    assert sorted(sealed["native_inventory"]["derived_items"]) == [
        '["node","entity-1"]',
        '["relationship","edge-1"]',
    ]
    assert sealed["native_inventory"]["raw_original_only"] is False
    assert sealed["cells"] == []
    assert not (output / "packs").exists()
    # The inventory that refused the checkpoint is still on disk as evidence.
    assert json.loads((output / checkpoints.INVENTORY_NAME).read_text())["items"] == state.items
    assert state.calls == []


def test_checkpoint_one_accepts_and_counts_the_consolidated_inventory(
    tmp_path: Path, state: Any
) -> None:
    state.items, state.inventory = consolidated_inventory()
    output = tmp_path / "cp1"

    assert run(1, output, prior_root=None) == 2

    inventory = receipt_of(output)["native_inventory"]
    assert inventory["source_counts"] == {
        "entity": 1,
        "episode": 0,
        "relationship": 1,
        "raw_capture": 2,
    }
    assert inventory["authorized_kinds"] == {"graph_entity": 1, "raw_capture": 2, "relationship": 1}
    assert inventory["raw_item_count"] == 2
    assert inventory["sha256"] == contract.digest(state.items)


# ---------------------------------------------------------------------------
# The twenty-four cells
# ---------------------------------------------------------------------------


def test_twenty_four_receipts_carry_the_coordinates_and_catalog(tmp_path: Path, state: Any) -> None:
    output = tmp_path / "cp0"

    assert run(0, output) == 0

    written = sorted(p.relative_to(output).as_posix() for p in output.rglob("packs/**/*.json"))
    assert len(written) == 24
    for task in contract.TASKS:
        for arm in contract.ARMS:
            document = pack_of(output, 0, task, arm)
            assert (document["checkpoint"], document["task"], document["arm"]) == (0, task, arm)
            assert document["catalog_sha256"] == SCHEDULE_CATALOG_SHA256
            assert set(document) >= MATERIALIZE_KEYS
    assert pack_of(output, 0, contract.TASKS[0], "no_memory")["memory"] == ""

    sealed = receipt_of(output)
    assert sealed["prepared"] == sealed["denominator"] == 24
    assert sealed["status"] == checkpoints.STATUS_PREPARED
    assert len(sealed["cells"]) == 24
    assert {(row["task"], row["arm"]) for row in sealed["cells"]} == {
        (task, arm) for task in contract.TASKS for arm in contract.ARMS
    }
    assert sealed["missing_reasons"] == {}
    assert sealed["catalog_sha256"] == SCHEDULE_CATALOG_SHA256
    assert sealed["schedule_source_catalog_sha256"] == checkpoints.schedule_catalog_sha256()
    assert sealed["tokenizer"]["tokenizer_sha256"] == "fixture"
    assert state.counters[0].verified == 1

    catalog = json.loads((output / checkpoints.CATALOG_NAME).read_text(encoding="utf-8"))
    assert catalog["source_count"] == contract.SOURCE_COUNT == len(catalog["source_ids"])
    assert catalog["catalog_sha256"] == SCHEDULE_CATALOG_SHA256


def test_a_prepared_receipt_has_a_byte_identical_memory_sibling(tmp_path: Path, state: Any) -> None:
    output = tmp_path / "cp0"

    assert run(0, output) == 0

    for task in contract.TASKS:
        for arm in contract.ARMS:
            document = pack_of(output, 0, task, arm)
            sibling = output / "packs" / "cp0" / task / f"{arm}.txt"
            assert sibling.read_bytes() == document["memory"].encode()
    row = next(
        row
        for row in receipt_of(output)["cells"]
        if (row["task"], row["arm"]) == (contract.TASKS[0], "native")
    )
    memory = pack_of(output, 0, contract.TASKS[0], "native")["memory"]
    assert row["memory_sha256"] == contract.sha(memory.encode())
    assert row["memory_bytes"] == len(memory.encode()) > len(memory)


def test_a_missing_pack_writes_no_memory_sibling(tmp_path: Path, state: Any) -> None:
    state.missing = {(contract.TASKS[0], "raw_retrieval"), (contract.TASKS[2], "native")}
    output = tmp_path / "cp0"

    assert run(0, output) == 2

    for task, arm in sorted(state.missing):
        document = pack_of(output, 0, task, arm)
        assert document["status"] == "missing_pack"
        assert document["memory"] is None
        assert not (output / "packs" / "cp0" / task / f"{arm}.txt").exists()
    sealed = receipt_of(output)
    assert sealed["status"] == checkpoints.STATUS_PARTIAL
    assert sealed["prepared"] == 22
    assert sealed["missing_reasons"] == {"raw_ranked_source_changed": 2}
    missing_rows = [row for row in sealed["cells"] if row["status"] == "missing_pack"]
    assert all(row["memory_path"] is None and row["memory_sha256"] is None for row in missing_rows)


def test_partial_preparation_exits_two_and_still_writes_everything(
    tmp_path: Path, state: Any
) -> None:
    state.missing = {(contract.TASKS[1], "strong_summary")}
    output = tmp_path / "cp0"

    assert run(0, output) == 2

    assert len(list(output.rglob("packs/**/*.json"))) == 24
    assert len(list(output.rglob("packs/**/*.txt"))) == 23
    assert (output / checkpoints.CATALOG_NAME).is_file()
    assert (output / checkpoints.INVENTORY_NAME).is_file()
    sealed = receipt_of(output)
    assert sealed["prepared"] == 23
    assert sealed["started_at"]
    assert sealed["finished_at"] >= sealed["started_at"]


# ---------------------------------------------------------------------------
# Checkpoint 1 reuses the checkpoint-0 packs
# ---------------------------------------------------------------------------


def test_checkpoint_one_hands_the_prior_to_raw_and_summary_only(tmp_path: Path, state: Any) -> None:
    zero = tmp_path / "cp0"
    assert run(0, zero) == 0
    state.calls.clear()
    state.items, state.inventory = consolidated_inventory()
    one = tmp_path / "cp1"

    assert run(1, one, prior_root=zero) == 0

    for call in state.calls:
        prior_expected = call["arm"] in checkpoints.CP1_PRIOR_ARMS
        assert (call["prior"] is not None) is prior_expected, call["arm"]
        if not prior_expected:
            continue
        prior = call["prior"]
        assert prior == pack_of(zero, 0, call["task"], call["arm"])
        assert (prior["checkpoint"], prior["task"], prior["arm"]) == (0, call["task"], call["arm"])
        assert call["prior_sha256"] == contract.digest(prior)
    assert sum(call["prior"] is not None for call in state.calls) == 12

    rows = {(row["task"], row["arm"]): row for row in receipt_of(one)["cells"]}
    assert rows[(contract.TASKS[0], "raw_retrieval")]["prior"]["status"] == "loaded"
    assert rows[(contract.TASKS[0], "native")]["prior"] is None


def test_checkpoint_one_without_a_prior_root_leaves_those_cells_missing(
    tmp_path: Path, state: Any
) -> None:
    output = tmp_path / "cp1"

    assert run(1, output, prior_root=None) == 2

    sealed = receipt_of(output)
    assert sealed["prepared"] == 12
    assert sealed["missing_reasons"] == {"qualified_checkpoint_zero_pack_missing": 12}
    rows = {(row["task"], row["arm"]): row for row in sealed["cells"]}
    assert rows[(contract.TASKS[0], "strong_summary")]["prior"] == {
        "status": "no_prior_root",
        "path": None,
        "sha256": None,
    }


def test_an_absent_prior_receipt_is_never_invented(tmp_path: Path, state: Any) -> None:
    zero = tmp_path / "cp0"
    assert run(0, zero) == 0
    (zero / "packs" / "cp0" / contract.TASKS[0] / "raw_retrieval.json").unlink()
    one = tmp_path / "cp1"

    assert run(1, one, prior_root=zero) == 2

    rows = {(row["task"], row["arm"]): row for row in receipt_of(one)["cells"]}
    absent = rows[(contract.TASKS[0], "raw_retrieval")]
    assert absent["prior"]["status"] == "absent"
    assert absent["status"] == "missing_pack"
    assert not (one / "packs" / "cp0" / contract.TASKS[0] / "raw_retrieval.txt").exists()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_the_checkpoint_receipt_is_created_exclusively(tmp_path: Path, state: Any) -> None:
    output = tmp_path / "cp0"
    assert run(0, output) == 0
    before = (output / checkpoints.RECEIPT_NAME).read_bytes()

    with pytest.raises(checkpoints.CheckpointError, match="refusing to overwrite"):
        asyncio.run(
            checkpoints.prepare_checkpoint(
                0, output=output, tokenizer_assets=tmp_path, prior_root=None
            )
        )

    assert run(0, output) == 2
    assert (output / checkpoints.RECEIPT_NAME).read_bytes() == before


@pytest.mark.asyncio
async def test_a_catalog_the_schedule_does_not_know_fails_the_phase(
    tmp_path: Path, state: Any
) -> None:
    state.catalog_sha256 = "f" * 64
    output = tmp_path / "cp0"

    with pytest.raises(checkpoints.CheckpointError, match="disagree"):
        await checkpoints.prepare_checkpoint(
            0, output=output, tokenizer_assets=tmp_path, prior_root=None
        )

    sealed = receipt_of(output)
    assert sealed["status"] == checkpoints.STATUS_CATALOG_DISAGREES
    assert sealed["catalog_sha256"] == "f" * 64
    assert not (output / "packs").exists()
    assert state.natives == []


@pytest.mark.asyncio
async def test_an_incomplete_catalog_fails_the_phase(tmp_path: Path, state: Any) -> None:
    state.source_count = contract.SOURCE_COUNT - 1
    output = tmp_path / "cp0"

    with pytest.raises(checkpoints.CheckpointError, match="232 originals"):
        await checkpoints.prepare_checkpoint(
            0, output=output, tokenizer_assets=tmp_path, prior_root=None
        )

    assert receipt_of(output)["status"] == checkpoints.STATUS_ERROR


@pytest.mark.asyncio
async def test_an_unknown_checkpoint_is_refused(tmp_path: Path, state: Any) -> None:
    with pytest.raises(checkpoints.CheckpointError, match="unknown checkpoint"):
        await checkpoints.prepare_checkpoint(
            2, output=tmp_path / "cp2", tokenizer_assets=tmp_path, prior_root=None
        )


def test_a_receipt_outside_its_cell_is_refused() -> None:
    pack = {"status": "prepared", "memory": "", "checkpoint": 1, "task": contract.TASKS[0]}
    with pytest.raises(checkpoints.CheckpointError, match="disagrees on checkpoint"):
        checkpoints.coordinate(
            pack, checkpoint=0, task=contract.TASKS[0], arm="no_memory", catalog="a" * 64
        )


def test_an_unprepared_pack_may_not_carry_memory() -> None:
    pack = {"status": "missing_pack", "reason": "raw_required_lane_incomplete", "memory": "text"}
    with pytest.raises(checkpoints.CheckpointError, match="carries memory"):
        checkpoints.coordinate(
            pack, checkpoint=0, task=contract.TASKS[0], arm="raw_retrieval", catalog="a" * 64
        )


# ---------------------------------------------------------------------------
# Bindings that need no eval host
# ---------------------------------------------------------------------------


def test_the_reader_is_the_private_owner_scope(tmp_path: Path, state: Any) -> None:
    output = tmp_path / "cp0"
    assert run(0, output) == 0

    sealed = receipt_of(output)
    assert sealed["reader"] == {
        "principal_id": contract.PRINCIPAL_ID,
        "project": None,
        "memory_scope": "private",
        "scope_key": None,
    }
    assert sealed["group_id"] == contract.ORGANIZATION_ID
    assert state.qualified == [(contract.ORGANIZATION_ID, contract.PRINCIPAL_ID)]
    assert "private" in sealed["reader_scope_evidence"]


def test_the_summary_library_validator_only_rebinds_the_accepted_library(
    tmp_path: Path,
) -> None:
    references, validate = checkpoints.summary_library()
    assert len(references) == contract.FAMILY_COUNT

    catalog_receipt = {"source_ids": ["a"]}
    assert validate(references, catalog_receipt) == {
        "references_sha256": contract.digest(references),
        "catalog_sha256": contract.digest(catalog_receipt),
    }

    rewritten = {**references, sorted(references)[0]: {"text": "invented"}}
    with pytest.raises(ValueError, match="not_the_accepted_library"):
        validate(rewritten, catalog_receipt)


def test_the_host_binding_names_every_owner(tmp_path: Path) -> None:
    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"archive": {"path": "/a", "sha256": "b"}, "source": {}}))
    with pytest.raises(checkpoints.CheckpointError, match="host binding keys"):
        checkpoints.host_binding(path)

    path.write_text(
        json.dumps({"archive": {"path": "/a", "sha256": "b"}, "source": {}, "owners": {}})
    )
    with pytest.raises(checkpoints.CheckpointError, match="owner keys"):
        checkpoints.host_binding(path)


def test_the_schedule_catalog_digest_is_the_pinned_one() -> None:
    assert checkpoints.schedule_catalog_sha256() == SCHEDULE_CATALOG_SHA256


def test_the_written_packs_are_the_ones_materialize_reads(tmp_path: Path, state: Any) -> None:
    """The consumer, not a restatement of it, decides whether this layout is right."""
    state.missing = {(contract.TASKS[0], "raw_retrieval")}
    output = tmp_path / "cp0"
    assert run(0, output) == 2

    schedule = {"source_catalog_sha256": SCHEDULE_CATALOG_SHA256}
    prepared = materialize._read_pack(
        output, schedule, {"checkpoint": 0, "task": contract.TASKS[0], "arm": "native"}
    )
    assert prepared["status"] == "prepared"
    assert prepared["memory"] == prepared["receipt"]["memory"].encode()

    unprepared = materialize._read_pack(
        output, schedule, {"checkpoint": 0, "task": contract.TASKS[0], "arm": "raw_retrieval"}
    )
    assert unprepared["status"] == "missing_pack"
    assert unprepared["reason"] == "raw_ranked_source_changed"

    with pytest.raises(materialize.ManifestError, match="another source catalog"):
        materialize._read_pack(
            output,
            {"source_catalog_sha256": "0" * 64},
            {"checkpoint": 0, "task": contract.TASKS[0], "arm": "native"},
        )
