"""Diagnostic intervention: arms recomposed from sealed packs, never retrieved.

No provider, no database and no tokenizer. Source packs are written in the
layout a prepare-only probe seals, the counter measures characters, and the
runner is a fake, so every assertion is about the recomposition itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks.agent_tasks import runner
from benchmarks.agent_tasks.manifest import ManifestError, digest, identity, runtime_identity
from benchmarks.agent_tasks.screen48 import checkpoints, contract, headroom, intervention, probe

TASK = "alpha-task"
FAMILY = "decoded-root-routing"
HEADER = contract.EPISODE_HEADER
PATTERN_ID = json.dumps(["pattern", "pattern_v3_escape"], separators=(",", ":"))
SUMMARY_TEXT = "unquote alone does not reject a malformed escape such as %ZZ."
ORACLE = json.dumps(
    {
        "schema_version": "sibyl-json-cli-cases-v1",
        "cases": [{"id": "one", "input": {"a": 1}, "expected": {"b": 2}}],
    },
    sort_keys=True,
)


def episode(name: str) -> str:
    return (
        f'<historical-episode id="{name}" sha256="{"0" * 64}" renderer="test">\n'
        f"episode {name}: the router decoded twice\n</historical-episode>\n"
    )


def derived_block() -> str:
    body = {"id": "pattern_v3_escape", "content": "reject %ZZ; decode each segment once"}
    return "<native>\n" + json.dumps(body, sort_keys=True) + "\n</native>\n"


def receipt(identifier: str, text: str, *, rendered: bool) -> dict[str, Any]:
    entry = {"id": identifier, "block_sha256": contract.sha(text.encode())}
    if rendered:
        entry = {**entry, "block_sha256": "b" * 64, "rendered_sha256": contract.sha(text.encode())}
    return entry


def native_raw_key(name: str) -> str:
    return json.dumps(["raw_memory", f"raw_memory:{name}"], separators=(",", ":"))


class CharCounter:
    """Stand in for the tokenizer counter: a memory fits while it stays under ``limit``."""

    def __init__(self, limit: int) -> None:
        self.limit = limit

    def request(self, prompt: str, memory: str, workspace: dict[str, bytes]) -> dict[str, Any]:
        del prompt, workspace
        return {"fits": len(memory) <= self.limit, "memory_tokens": len(memory)}


def write_material(root: Path) -> Path:
    directory = root / "tasks" / TASK
    (directory / "workspace").mkdir(parents=True, exist_ok=True)
    (directory / "prompt.md").write_text(f"# {TASK}\n\nRepair the router.\n")
    (directory / "oracle.json").write_text(ORACLE)
    (directory / "workspace" / "app.py").write_text("# entry point\n")
    catalog = {
        "schema_version": "sibyl-transfer-task-material-v1",
        "tasks": [
            {
                "id": TASK,
                "family_id": f"transfer-{TASK}",
                "learning_family": FAMILY,
                "split": "development",
            }
        ],
    }
    (root / headroom.CATALOG_NAME).write_text(json.dumps(catalog, indent=2, sort_keys=True))
    return root


def write_source(root: Path, *, with_derived: bool = True) -> Path:
    """A prepare-only probe's output: native and raw packs for one task, sealed."""
    native_items = [(native_raw_key("e1"), episode("e1"), True)]
    if with_derived:
        native_items.insert(0, (PATTERN_ID, derived_block(), False))
    raw_items = [
        ("e1", episode("e1"), True),
        ("e2", episode("e2"), True),
        ("e3", episode("e3"), True),
    ]
    cells = []
    for arm, items in (("native", native_items), ("raw_retrieval", raw_items)):
        memory = HEADER + "".join(text for _, text, _ in items)
        document = {
            "schema": "sibyl-unarmed-whole-item-pack-v3",
            "checkpoint": 1,
            "task": TASK,
            "arm": arm,
            "status": "prepared",
            "reason": None,
            "memory": memory,
            "counts": {"memory_tokens": len(memory), "fits": True},
            "selected": [receipt(i, t, rendered=r) for i, t, r in items],
        }
        paths = checkpoints._write_pack(root, document)
        cells.append(
            {
                "checkpoint": 1,
                "task": TASK,
                "arm": arm,
                "status": "prepared",
                "memory_sha256": contract.sha(memory.encode()),
                **paths,
            }
        )
    preparation = {
        "schema": probe.PREPARATION_SCHEMA,
        "checkpoint": 1,
        "tasks": [TASK],
        "arms": ["native", "raw_retrieval"],
        "families": {TASK: f"transfer-{TASK}"},
        "cells": cells,
        "catalog_sha256": "c" * 64,
        "native_inventory": {"derived_items": [PATTERN_ID] if with_derived else []},
    }
    (root / probe.PREPARATION_NAME).write_text(json.dumps(preparation, sort_keys=True))
    return root


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch):
    def install(limit: int = 10_000) -> None:
        monkeypatch.setattr(checkpoints, "build_counter", lambda assets: CharCounter(limit))
        references = {
            FAMILY: {"text": SUMMARY_TEXT, "text_sha256": contract.sha(SUMMARY_TEXT.encode())}
        }
        monkeypatch.setattr(checkpoints, "summary_library", lambda: (references, None))

    return install


def build(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    arguments = {
        "preparation_root": write_source(tmp_path / "source"),
        "output": tmp_path / "out",
        "task": TASK,
        "arms": list(intervention.DIAGNOSTIC_ARMS),
        "family": FAMILY,
        "tokenizer_assets": tmp_path,
        "tasks_root": write_material(tmp_path / "material"),
    }
    return intervention.build(**{**arguments, **overrides})


def memory_of(root: Path, preparation: dict[str, Any], arm: str) -> tuple[str, dict[str, Any]]:
    cell = next(cell for cell in preparation["cells"] if cell["arm"] == arm)
    document = json.loads((root / cell["receipt_path"]).read_text())
    assert (root / cell["memory_path"]).read_text() == document["memory"]
    assert contract.sha(document["memory"].encode()) == cell["memory_sha256"]
    return document["memory"], document


def test_split_recovers_each_item_and_refuses_a_tampered_pack() -> None:
    texts = [derived_block(), episode("e1")]
    selected = [
        receipt(PATTERN_ID, texts[0], rendered=False),
        receipt(native_raw_key("e1"), texts[1], rendered=True),
    ]
    memory = HEADER + "".join(texts)
    assert intervention.split_items(memory, HEADER, selected) == texts
    with pytest.raises(ManifestError, match="does not match its digest"):
        intervention.split_items(memory.replace("twice", "once"), HEADER, selected)
    with pytest.raises(ManifestError, match="after its last selected item"):
        intervention.split_items(memory + episode("e9"), HEADER, selected)


def test_arms_are_recompositions_of_the_sealed_packs(tmp_path: Path, seams) -> None:
    seams()
    preparation = build(tmp_path)
    out = tmp_path / "out"
    source = tmp_path / "source"
    assert preparation["prepared"] == len(intervention.DIAGNOSTIC_ARMS)
    assert preparation["derived_items"] == [PATTERN_ID]
    for arm in intervention.SOURCE_ARMS:
        memory, _ = memory_of(out, preparation, arm)
        assert memory == (source / "packs" / "cp1" / TASK / f"{arm}.txt").read_text()
    minus, document = memory_of(out, preparation, intervention.MINUS_DERIVED_ARM)
    assert minus == HEADER + episode("e1")
    assert document["intervention"]["removed"] == [PATTERN_ID]
    plus, document = memory_of(out, preparation, intervention.PLUS_DERIVED_ARM)
    assert plus == HEADER + derived_block() + episode("e1") + episode("e2") + episode("e3")
    assert document["intervention"]["added"] == [PATTERN_ID]
    summary, _ = memory_of(out, preparation, intervention.PLUS_SUMMARY_ARM)
    assert summary.startswith(HEADER + f'<summary id="{FAMILY}"')
    assert SUMMARY_TEXT in summary
    assert preparation["learning_benefit_established"] is False


def test_an_added_item_displaces_raw_items_from_the_tail_under_the_same_budget(
    tmp_path: Path, seams
) -> None:
    raw_memory = HEADER + episode("e1") + episode("e2") + episode("e3")
    seams(limit=len(raw_memory))
    preparation = build(tmp_path)
    plus, document = memory_of(tmp_path / "out", preparation, intervention.PLUS_DERIVED_ARM)
    assert len(plus) <= len(raw_memory)
    assert plus.startswith(HEADER + derived_block())
    assert document["intervention"]["dropped"] == ["e3"]
    assert document["intervention"]["kept"] == [PATTERN_ID, "e1", "e2"]


def test_a_native_pack_without_a_derived_item_is_refused(tmp_path: Path, seams) -> None:
    seams()
    with pytest.raises(ManifestError, match="carries no derived item"):
        build(tmp_path, preparation_root=write_source(tmp_path / "bare", with_derived=False))


def test_an_arm_outside_the_intervention_is_refused(tmp_path: Path, seams) -> None:
    seams()
    with pytest.raises(ManifestError, match="outside the intervention"):
        build(tmp_path, arms=["native", "no_memory"])


def write_template(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "uv.lock"
    lock.write_bytes(b"# frozen dependency lock\n")
    controller = root / "controller.py"
    controller.write_bytes(b"import sys\n\nsys.stdout.write('{}')\n")
    experience = root / "experience-0.json"
    experience.write_bytes(b'{"episode": 0}\n')
    return {
        "artifact_root": str(root),
        "runtime_sha256": identity(runtime_identity()),
        "dependency_lock": {"path": "uv.lock", "sha256": digest(lock.read_bytes())},
        "seed": 7,
        "controller": {
            "script": {"path": "controller.py", "sha256": digest(controller.read_bytes())},
            "args": [],
        },
        "controller_api_key_env": None,
        "controller_model": "qwen/qwen3-coder-next",
        "controller_tools": ["shell"],
        "controller_budget": {
            "input_tokens": 2000000,
            "output_tokens": 8000,
            "tool_calls": 20,
            "cost_usd": 2.0,
        },
        "controller_timeout_seconds": 600.0,
        "checker_timeout_seconds": 120.0,
        "checker": {
            "image": "sha256:" + "0" * 64,
            "docker": "/usr/bin/docker",
            "docker_host": "unix:///var/run/docker.sock",
            "argv": ["python", "-B", "app.py"],
            "memory_mb": 256,
            "timeout_seconds": 10.0,
        },
        "experiences": [
            {
                "id": "source-0",
                "family_id": "learning-family-0",
                "split": "learning",
                "revision": "1",
                "artifact": {
                    "path": "experience-0.json",
                    "sha256": digest(experience.read_bytes()),
                },
            }
        ],
    }


def test_the_diagnostic_arms_run_paired_under_one_seed_per_repetition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seams
) -> None:
    seams()
    calls: list[dict[str, Any]] = []

    def fake(manifest_path, *, task_id, arm_id, output, attempt_id=None, seed=None):
        calls.append({"arm": arm_id, "seed": seed, "manifest": manifest_path})
        output.mkdir(parents=True)
        result = {
            "attempt_id": attempt_id,
            "status": "passed",
            "success": True,
            "outcome": {"passed": True, "status": "passed"},
            "controller": {"elapsed_seconds": 1.0},
            "usage": {"input_tokens": 1, "output_tokens": 1, "tool_calls": 1.0, "cost_usd": 0.01},
            "receipt_sha256": digest(str(attempt_id).encode()),
        }
        (output / "receipt.json").write_text(json.dumps(result, sort_keys=True))
        return result

    monkeypatch.setattr(runner, "run_task", fake)
    template = write_template(tmp_path / "artifacts")
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps(template))
    exit_code = intervention.main(
        [
            "--preparation",
            str(write_source(tmp_path / "source")),
            "--task",
            TASK,
            "--family",
            FAMILY,
            "--tasks-root",
            str(write_material(tmp_path / "material")),
            "--template",
            str(template_path),
            "--tokenizer-assets",
            str(tmp_path),
            "--repetitions",
            "2",
            "--workers",
            "2",
            "--output",
            str(tmp_path / "out"),
        ]
    )
    assert exit_code == 0
    report = json.loads((tmp_path / "out" / probe.REPORT_NAME).read_text())
    assert {row["arm"] for row in report["rows"]} == set(intervention.DIAGNOSTIC_ARMS)
    for arm in intervention.DIAGNOSTIC_ARMS:
        assert sorted(call["seed"] for call in calls if call["arm"] == arm) == [7, 8]
    assert len({str(call["manifest"]) for call in calls}) == 1
