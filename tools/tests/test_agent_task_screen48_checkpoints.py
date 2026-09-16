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
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from benchmarks.agent_tasks.screen48 import checkpoints, contract, cycle, materialize
from benchmarks.agent_tasks.screen48.recall import whole_items

#: The frozen schedule's catalog digest, pinned here so a silent material or
#: database swap cannot quietly agree with itself.
SCHEDULE_CATALOG_SHA256 = "d2d406d004e71ced67ae7dd8af52d2b2bbd0ac6330cf8616b6c3d99f5567d487"

MATERIALIZE_KEYS = frozenset(
    {"status", "reason", "checkpoint", "task", "arm", "memory", "counts", "catalog_sha256"}
)

#: A value that is not a credential, used only to prove nothing writes it down.
OWNER_KEY_FIXTURE = "screen48-fixture-owner-value-not-a-credential"

#: 24 receipts, 24 memory siblings, the catalog, the inventory and the receipt.
CHECKPOINT_ARTIFACT_FLOOR = 50


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


class FakeSummaryCatalog:
    """The catalog surface ``summary_items`` reads: families, and a bound receipt."""

    def __init__(self, references: dict[str, Any]) -> None:
        self.rows = {
            source_id: {"training_family": family}
            for family, reference in references.items()
            for source_id in reference["source_ids"]
        }
        self.organization_id = contract.ORGANIZATION_ID
        self.catalog_sha256 = contract.digest(self.receipt())

    def receipt(self) -> dict[str, Any]:
        return {"source_ids": sorted(self.rows)}


class QuarterCounter:
    """A summary counter well under the policy ceiling, so the ceiling is not what fails."""

    def count(self, text: str) -> int:
        return len(text) // 4


def cell_pack(**overrides: Any) -> dict[str, Any]:
    """A preparation receipt carrying the four coordinates, before any tampering."""
    return {
        "checkpoint": 0,
        "task": contract.TASKS[0],
        "arm": "raw_retrieval",
        "catalog_sha256": SCHEDULE_CATALOG_SHA256,
        **overrides,
    }


def rewrite_json(path: Path, payload: Any) -> None:
    """Rewrite one sealed artifact in place, exactly as a tamperer would."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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

    # The digest handed to the adapter is the one checkpoint 0 sealed for that
    # cell, not one recomputed from the file the adapter is about to be given.
    sealed_zero = {(row["task"], row["arm"]): row for row in receipt_of(zero)["cells"]}
    for call in state.calls:
        prior_expected = call["arm"] in checkpoints.CP1_PRIOR_ARMS
        assert (call["prior"] is not None) is prior_expected, call["arm"]
        if not prior_expected:
            continue
        prior = call["prior"]
        assert prior == pack_of(zero, 0, call["task"], call["arm"])
        assert (prior["checkpoint"], prior["task"], prior["arm"]) == (0, call["task"], call["arm"])
        assert (
            call["prior_sha256"] == sealed_zero[(call["task"], call["arm"])]["pack_receipt_sha256"]
        )
    assert sum(call["prior"] is not None for call in state.calls) == 12

    receipt = receipt_of(one)
    assert receipt["prior_bindings"] == {
        "root": str(zero),
        "bound_cells": 24,
        "unbound_reason": None,
    }
    rows = {(row["task"], row["arm"]): row for row in receipt["cells"]}
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


def test_a_rewritten_prior_pack_is_unbound_and_never_reused(tmp_path: Path, state: Any) -> None:
    """A cp0 pack rewritten on disk no longer matches what cp0 sealed for that cell."""
    zero = tmp_path / "cp0"
    assert run(0, zero) == 0
    path = zero / "packs" / "cp0" / contract.TASKS[0] / "raw_retrieval.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["memory"] = "pack cp0 venue-capacity-report raw_retrieval, rewritten by hand\n"
    tampered["counts"] = {"memory_tokens": 7, "fits": True}
    rewrite_json(path, tampered)
    state.calls.clear()
    state.items, state.inventory = consolidated_inventory()
    one = tmp_path / "cp1"

    assert run(1, one, prior_root=zero) == 2

    sealed = receipt_of(one)
    rows = {(row["task"], row["arm"]): row for row in sealed["cells"]}
    row = rows[(contract.TASKS[0], "raw_retrieval")]
    assert row["prior"] == {
        "status": checkpoints.PRIOR_UNBOUND,
        "reason": "prior_receipt_digest_mismatch",
        "path": str(path),
        "sha256": contract.digest(tampered),
    }
    assert row["status"] == "missing_pack"
    assert row["reason"] == "qualified_checkpoint_zero_pack_missing"
    assert sealed["prepared"] == 23
    assert sealed["missing_reasons"] == {"qualified_checkpoint_zero_pack_missing": 1}
    # The adapter was never handed the rewritten pack, and the other eleven
    # reusing cells still bind, so the refusal is the tampering and nothing else.
    handed = {(call["task"], call["arm"]): call for call in state.calls}
    assert handed[(contract.TASKS[0], "raw_retrieval")]["prior"] is None
    assert handed[(contract.TASKS[0], "raw_retrieval")]["prior_sha256"] is None
    assert sum(call["prior"] is not None for call in state.calls) == 11


def test_a_prior_root_sealed_against_another_catalog_binds_nothing(
    tmp_path: Path, state: Any
) -> None:
    zero = tmp_path / "cp0"
    assert run(0, zero) == 0
    sealed_zero = json.loads((zero / checkpoints.RECEIPT_NAME).read_text(encoding="utf-8"))
    sealed_zero["catalog_sha256"] = "0" * 64
    rewrite_json(zero / checkpoints.RECEIPT_NAME, sealed_zero)
    state.calls.clear()
    one = tmp_path / "cp1"

    assert run(1, one, prior_root=zero) == 2

    sealed = receipt_of(one)
    assert sealed["prior_bindings"] == {
        "root": str(zero),
        "bound_cells": 0,
        "unbound_reason": "prior_checkpoint_receipt_catalog_disagrees",
    }
    assert sealed["prepared"] == 12
    assert sealed["missing_reasons"] == {"qualified_checkpoint_zero_pack_missing": 12}
    rows = {(row["task"], row["arm"]): row for row in sealed["cells"]}
    assert rows[(contract.TASKS[0], "strong_summary")]["prior"] == {
        "status": checkpoints.PRIOR_UNBOUND,
        "reason": "prior_checkpoint_receipt_catalog_disagrees",
        "path": str(zero / "packs" / "cp0" / contract.TASKS[0] / "strong_summary.json"),
        "sha256": None,
    }
    assert all(call["prior"] is None for call in state.calls)


def test_a_prior_root_that_is_not_checkpoint_zero_binds_nothing(tmp_path: Path) -> None:
    root = tmp_path / "cp1-as-prior"
    root.mkdir()
    rewrite_json(
        root / checkpoints.RECEIPT_NAME,
        {"checkpoint": 1, "catalog_sha256": SCHEDULE_CATALOG_SHA256, "cells": []},
    )
    assert checkpoints.prior_bindings(root, catalog_sha256=SCHEDULE_CATALOG_SHA256) == (
        {},
        "prior_checkpoint_receipt_is_not_checkpoint_zero",
    )

    empty = tmp_path / "empty"
    empty.mkdir()
    assert checkpoints.prior_bindings(empty, catalog_sha256=SCHEDULE_CATALOG_SHA256) == (
        {},
        "prior_checkpoint_receipt_absent",
    )

    (root / checkpoints.RECEIPT_NAME).write_text("{not json", encoding="utf-8")
    assert checkpoints.prior_bindings(root, catalog_sha256=SCHEDULE_CATALOG_SHA256) == (
        {},
        "prior_checkpoint_receipt_unreadable",
    )


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


def test_a_queue_that_will_not_drain_fails_a_full_checkpoint(
    tmp_path: Path, state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twenty-four prepared cells and a wedged runtime is not a complete checkpoint."""

    async def wedged() -> None:
        raise RuntimeError("the job queue did not drain")

    monkeypatch.setattr(cycle, "shutdown_runtime", wedged)
    output = tmp_path / "cp0"

    assert run(0, output) == 2

    sealed = receipt_of(output)
    assert sealed["prepared"] == sealed["denominator"] == 24
    assert sealed["status"] == checkpoints.STATUS_PREPARED
    assert sealed["errors"] == ["shutdown:RuntimeError: the job queue did not drain"]


def test_an_unreadable_schedule_still_seals_a_receipt(
    tmp_path: Path, state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt is reserved before the first binding is read, so nothing runs unrecorded."""

    def unreadable() -> str:
        raise checkpoints.CheckpointError("the frozen schedule names no source catalog")

    monkeypatch.setattr(checkpoints, "schedule_catalog_sha256", unreadable)
    output = tmp_path / "cp0"

    assert run(0, output) == 2

    sealed = receipt_of(output)
    assert sealed["status"] == checkpoints.STATUS_ERROR
    assert sealed["schedule_source_catalog_sha256"] is None
    assert sealed["errors"] == ["CheckpointError: the frozen schedule names no source catalog"]
    assert sealed["finished_at"] >= sealed["started_at"]
    assert state.qualified == []


def test_an_unexpected_failure_exits_two_rather_than_tracing_back(
    tmp_path: Path, state: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def blows_up() -> None:
        raise ZeroDivisionError("the runtime came apart")

    monkeypatch.setattr(cycle, "bootstrap_runtime", blows_up)
    output = tmp_path / "cp0"

    assert run(0, output) == 2

    sealed = receipt_of(output)
    assert sealed["status"] == checkpoints.STATUS_ERROR
    assert sealed["errors"] == ["ZeroDivisionError: the runtime came apart"]
    assert "ZeroDivisionError: the runtime came apart" in capsys.readouterr().err


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
    pack = cell_pack(status="missing_pack", reason="raw_required_lane_incomplete", memory="text")
    with pytest.raises(checkpoints.CheckpointError, match="carries memory"):
        checkpoints.coordinate(
            pack,
            checkpoint=0,
            task=contract.TASKS[0],
            arm="raw_retrieval",
            catalog=SCHEDULE_CATALOG_SHA256,
        )


@pytest.mark.parametrize("absent", ["catalog_sha256", "arm", "task", "checkpoint"])
def test_a_receipt_that_names_no_coordinate_is_refused(absent: str) -> None:
    """A silent stamp would invent the coordinates this module promises never to invent."""
    pack = cell_pack(status="prepared", memory="text", counts={"fits": True})
    del pack[absent]

    with pytest.raises(checkpoints.CheckpointError, match=f"disagrees on {absent}"):
        checkpoints.coordinate(
            pack,
            checkpoint=0,
            task=contract.TASKS[0],
            arm="raw_retrieval",
            catalog=SCHEDULE_CATALOG_SHA256,
        )


def test_the_no_memory_arm_carries_the_empty_string_and_nothing_else() -> None:
    carrying = cell_pack(arm="no_memory", status="prepared", memory="a leaked summary")
    with pytest.raises(checkpoints.CheckpointError, match="no-memory arm carries memory"):
        checkpoints.coordinate(
            carrying,
            checkpoint=0,
            task=contract.TASKS[0],
            arm="no_memory",
            catalog=SCHEDULE_CATALOG_SHA256,
        )

    empty = cell_pack(arm="no_memory", status="prepared", memory="")
    document = checkpoints.coordinate(
        empty,
        checkpoint=0,
        task=contract.TASKS[0],
        arm="no_memory",
        catalog=SCHEDULE_CATALOG_SHA256,
    )
    assert document["memory"] == ""
    assert document["status"] == "prepared"


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


def test_the_vendored_library_builds_items_through_the_real_item_builder() -> None:
    """The real validator, composed with the real ``summary_items``, on the real library."""
    references, validate = checkpoints.summary_library()
    catalog = cast(whole_items.OriginalCatalog, FakeSummaryCatalog(references))
    assert len(catalog.rows) == contract.SOURCE_COUNT

    items = whole_items.summary_items(references, catalog, QuarterCounter(), validate)

    assert [item.id for item in items] == sorted(references)
    assert len(items) == contract.FAMILY_COUNT
    first = sorted(references)[0]
    assert items[0].block.startswith(f'<summary id="{first}" sha256=')
    assert references[first]["text"] in items[0].block
    # The evidence travels without the summary text it already carries in the block.
    assert "text" not in items[0].evidence
    assert items[0].evidence["text_sha256"] == references[first]["text_sha256"]

    altered = {
        **references,
        first: {**references[first], "text": references[first]["text"] + " and one more claim"},
    }
    with pytest.raises(whole_items.MissingPack, match="not_the_accepted_library"):
        whole_items.summary_items(altered, catalog, QuarterCounter(), validate)


def bind_owner_key_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth: Any,
    context: Any,
    allows_rest: bool = True,
) -> list[str]:
    """Stand in for the product's own API-key path, recording the key it was handed."""
    seen: list[str] = []

    async def authenticate_api_key(raw_key: str) -> Any:
        seen.append(raw_key)
        return auth

    async def resolve_auth_context(*, claims: Any) -> Any:
        return context

    monkeypatch.setitem(
        sys.modules,
        "sibyl.auth.dependencies",
        SimpleNamespace(
            _api_key_allows_rest=lambda *, scopes, method: allows_rest,
            _api_key_claims=lambda auth, *, scopes: {"scopes": scopes},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "sibyl.persistence.auth_runtime",
        SimpleNamespace(
            authenticate_api_key=authenticate_api_key,
            resolve_auth_context=resolve_auth_context,
        ),
    )
    return seen


def owner_context(organization_id: str = contract.ORGANIZATION_ID) -> SimpleNamespace:
    return SimpleNamespace(organization_id=organization_id, user_id=contract.PRINCIPAL_ID)


@pytest.mark.asyncio
async def test_the_owner_key_path_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    authenticated = SimpleNamespace(scopes=["memory:read"])
    monkeypatch.delenv(checkpoints.OWNER_API_KEY_ENV, raising=False)
    bind_owner_key_path(monkeypatch, auth=authenticated, context=owner_context())
    with pytest.raises(checkpoints.CheckpointError, match="must carry the study organization"):
        await checkpoints.authenticated_intake(contract.ORGANIZATION_ID, contract.PRINCIPAL_ID)

    monkeypatch.setenv(checkpoints.OWNER_API_KEY_ENV, OWNER_KEY_FIXTURE)
    bind_owner_key_path(monkeypatch, auth=None, context=owner_context())
    with pytest.raises(checkpoints.CheckpointError, match="did not authenticate"):
        await checkpoints.authenticated_intake(contract.ORGANIZATION_ID, contract.PRINCIPAL_ID)

    bind_owner_key_path(monkeypatch, auth=authenticated, context=owner_context(), allows_rest=False)
    with pytest.raises(checkpoints.CheckpointError, match="cannot read through REST"):
        await checkpoints.authenticated_intake(contract.ORGANIZATION_ID, contract.PRINCIPAL_ID)

    elsewhere = owner_context(organization_id="6f2f1a6c-0000-4000-8000-000000000000")
    bind_owner_key_path(monkeypatch, auth=authenticated, context=elsewhere)
    with pytest.raises(checkpoints.CheckpointError, match="belongs to another organization"):
        await checkpoints.authenticated_intake(contract.ORGANIZATION_ID, contract.PRINCIPAL_ID)

    context = owner_context()
    seen = bind_owner_key_path(monkeypatch, auth=authenticated, context=context)
    assert (
        await checkpoints.authenticated_intake(contract.ORGANIZATION_ID, contract.PRINCIPAL_ID)
        is context
    )
    assert seen == [OWNER_KEY_FIXTURE]


def test_the_owner_key_never_reaches_the_output_tree(
    tmp_path: Path, state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(checkpoints.OWNER_API_KEY_ENV, OWNER_KEY_FIXTURE)
    output = tmp_path / "cp0"

    assert run(0, output) == 0

    written = [path for path in output.rglob("*") if path.is_file()]
    assert len(written) > CHECKPOINT_ARTIFACT_FLOOR
    leaked = [
        path
        for path in written
        if OWNER_KEY_FIXTURE in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert leaked == []


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
