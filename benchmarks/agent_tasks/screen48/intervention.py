"""Diagnostic intervention: does one learned item change what the solver does?

The floor probe says whether memory arms lift a task. This says why, for one
task and the derived items a consolidation run produced, by recomposing packs
that a prepare-only probe already sealed over one database state. Nothing here
retrieves, ranks or generates memory; every arm is built from sealed bytes.

- ``native`` and ``raw_retrieval`` are the prepared packs, byte for byte.
- ``native_minus_derived`` is the native pack with the targeted derived items
  removed and nothing refilled, so any change is attributable to those items.
- ``raw_plus_derived`` puts the targeted items first and then the raw pack's
  items in their ranked order, prefix-packed under the same counter and budget
  the preparation used; raw items that no longer fit are recorded as evicted.
- ``raw_minus_evicted`` is the raw pack without the items ``raw_plus_derived``
  evicted, and nothing added, so eviction can be told apart from addition.
- ``raw_plus_family_summary`` puts the training family's summary-library
  reference first in the same way.

The targets are named by their native item keys; with none named, every
derived item in the native pack is targeted and the preparation says so.

The three recomposed arms are diagnostic interventions, not production arms,
and a difference between any two arms here is not a learning claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from benchmarks.agent_tasks.manifest import ManifestError, strict_json
from benchmarks.agent_tasks.screen48 import checkpoints, contract, probe

SCHEMA = "sibyl-screen48-intervention-v1"
NATIVE_ARM = "native"
RAW_ARM = "raw_retrieval"
MINUS_DERIVED_ARM = "native_minus_derived"
PLUS_DERIVED_ARM = "raw_plus_derived"
PLUS_SUMMARY_ARM = "raw_plus_family_summary"
MINUS_EVICTED_ARM = "raw_minus_evicted"
DIAGNOSTIC_ARMS = (
    NATIVE_ARM,
    MINUS_DERIVED_ARM,
    RAW_ARM,
    PLUS_DERIVED_ARM,
    MINUS_EVICTED_ARM,
    PLUS_SUMMARY_ARM,
)
SOURCE_ARMS = (NATIVE_ARM, RAW_ARM)
#: Every packable item's text ends with one of these, so a sealed pack can be
#: split back into items and each piece checked against its recorded digest.
ITEM_CLOSERS = ("</historical-episode>\n", "</native>\n", "</summary>\n")


def split_items(memory: str, header: str, selected: Sequence[dict[str, Any]]) -> list[str]:
    """Recover the ordered item texts of a sealed pack, verified by digest.

    A pack is its header followed by each selected item's text. The receipt
    records each text's digest (the rendering for an episode, the block for
    anything else), so a split is accepted only when every piece hashes to the
    digest recorded for it and nothing is left over.
    """
    if not memory.startswith(header):
        raise ManifestError("sealed pack does not open with the expected header")
    rest = memory[len(header) :]
    texts = []
    for receipt in selected:
        want = receipt.get("rendered_sha256") or receipt["block_sha256"]
        for end in _item_ends(rest):
            if contract.sha(rest[:end].encode()) == want:
                texts.append(rest[:end])
                rest = rest[end:]
                break
        else:
            raise ManifestError(f"sealed pack item {receipt['id']} does not match its digest")
    if rest:
        raise ManifestError("sealed pack holds bytes after its last selected item")
    return texts


def _item_ends(text: str) -> list[int]:
    ends = set()
    for closer in ITEM_CLOSERS:
        start = 0
        while (found := text.find(closer, start)) != -1:
            ends.add(found + len(closer))
            start = found + 1
    return sorted(ends)


def compose(
    header: str,
    items: Sequence[tuple[str, str]],
    *,
    counter: Any,
    prompt: str,
    workspace: dict[str, bytes],
) -> dict[str, Any]:
    """Prefix-pack ``(id, text)`` items under the counter, as the preparation does.

    Items are taken in order until the first that no longer fits; everything
    from there on is recorded as dropped, never reordered to squeeze in. No
    items at all is a valid empty pack (an ablation can remove everything); only
    a first item too large for the budget is a missing pack.
    """
    memory = ""
    counts = counter.request(prompt, memory, workspace)
    if not items:
        return {"status": "prepared", "memory": "", "counts": counts, "kept": [], "dropped": []}
    kept: list[str] = []
    for index, (identifier, text) in enumerate(items):
        candidate = (memory or header) + text
        candidate_counts = counter.request(prompt, candidate, workspace)
        if not candidate_counts["fits"]:
            dropped = [item_id for item_id, _ in items[index:]]
            break
        memory, counts = candidate, candidate_counts
        kept.append(identifier)
    else:
        dropped = []
    if not kept:
        return {
            "status": "missing_pack",
            "memory": None,
            "counts": None,
            "kept": [],
            "dropped": dropped,
        }
    return {
        "status": "prepared",
        "memory": memory,
        "counts": counts,
        "kept": kept,
        "dropped": dropped,
    }


def is_derived(receipt: dict[str, Any]) -> bool:
    """A native pack item that is not one of the study's retained raw originals.

    The recall adapter hydrates a catalog original and records its evidence as
    ``{"native": ..., "original": ...}``; everything else it returned (a graph
    entity, or a raw row outside the catalog such as a reflection candidate)
    keeps the engine's own evidence. The native key's type cannot tell these
    apart, because a reflection candidate is a raw_memory row too.
    """
    evidence = receipt.get("evidence")
    if not isinstance(evidence, dict):
        raise ManifestError(f"native item {receipt.get('id')!r} carries no evidence")
    return "original" not in evidence


def summary_block(references: dict[str, Any], family: str) -> str:
    """The family's summary-library reference, framed exactly as the summary arm frames it."""
    reference = references.get(family)
    if not isinstance(reference, dict) or not isinstance(reference.get("text"), str):
        raise ManifestError(f"no summary-library reference for family {family!r}")
    text = reference["text"]
    if contract.sha(text.encode()) != reference["text_sha256"]:
        raise ManifestError(f"summary-library reference {family!r} does not match its digest")
    return f'<summary id="{family}" sha256="{reference["text_sha256"]}">\n{text}\n</summary>\n'


def _load_pack(root: Path, cell: dict[str, Any]) -> dict[str, Any]:
    """Read a sealed pack only if the receipt is the one the preparation recorded.

    The item metadata decides which bytes an intervention removes or adds, so
    the whole receipt, not only its memory text, has to match its recorded digest.
    """
    document = strict_json((root / cell["receipt_path"]).read_bytes())
    if contract.digest(document) != cell.get("pack_receipt_sha256"):
        raise ManifestError(
            f"source pack receipt differs from its recorded digest: {cell['receipt_path']}"
        )
    if any(document.get(key) != cell[key] for key in ("checkpoint", "task", "arm")):
        raise ManifestError(f"source pack receipt is outside its cell: {cell['receipt_path']}")
    if document.get("status") != "prepared" or not isinstance(document.get("memory"), str):
        raise ManifestError(f"source pack is not prepared: {cell['receipt_path']}")
    if contract.sha(document["memory"].encode()) != cell["memory_sha256"]:
        raise ManifestError(
            f"source pack memory differs from its recorded digest: {cell['receipt_path']}"
        )
    return document


def _cell(output: Path, document: dict[str, Any]) -> dict[str, Any]:
    paths = checkpoints._write_pack(output, document)
    memory = document["memory"] if document["status"] == "prepared" else None
    counts = document.get("counts") or {}
    return {
        "checkpoint": document["checkpoint"],
        "task": document["task"],
        "arm": document["arm"],
        "status": document["status"],
        "reason": document.get("reason"),
        "pack_receipt_sha256": contract.digest(document),
        "memory_sha256": contract.sha(memory.encode()) if memory is not None else None,
        "memory_bytes": len(memory.encode()) if memory is not None else None,
        "memory_tokens": counts.get("memory_tokens"),
        "fits": counts.get("fits"),
        **paths,
    }


def build(
    *,
    preparation_root: Path,
    output: Path,
    task: str,
    arms: Sequence[str],
    family: str,
    tokenizer_assets: Path,
    tasks_root: Path,
    targets: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Write the arms' packs under ``output`` and return a preparation ``run_cells`` accepts."""
    if unknown := sorted(set(arms) - set(DIAGNOSTIC_ARMS)):
        raise ManifestError(f"arms are outside the intervention: {unknown}")
    source = strict_json((preparation_root / probe.PREPARATION_NAME).read_bytes())
    by_cell = {(cell["task"], cell["arm"]): cell for cell in source["cells"]}
    missing = [arm for arm in SOURCE_ARMS if (task, arm) not in by_cell]
    if missing:
        raise ManifestError(f"the source preparation has no {missing} pack for {task}")
    packs = {arm: _load_pack(preparation_root, by_cell[(task, arm)]) for arm in SOURCE_ARMS}
    checkpoint = source["checkpoint"]
    header = contract.EPISODE_HEADER
    native_texts = split_items(packs[NATIVE_ARM]["memory"], header, packs[NATIVE_ARM]["selected"])
    raw_texts = split_items(packs[RAW_ARM]["memory"], header, packs[RAW_ARM]["selected"])
    native_items = list(zip(packs[NATIVE_ARM]["selected"], native_texts, strict=True))
    raw_items = [
        (receipt["id"], text)
        for receipt, text in zip(packs[RAW_ARM]["selected"], raw_texts, strict=True)
    ]
    present = {receipt["id"]: receipt for receipt, _ in native_items}
    if targets is not None:
        for target in targets:
            if target not in present:
                raise ManifestError(f"target {target} is not in the native pack for {task}")
            if not is_derived(present[target]):
                raise ManifestError(f"target {target} is a raw original, not a derived item")
    chosen = (
        set(targets) if targets is not None else {i for i, r in present.items() if is_derived(r)}
    )
    derived = [(receipt["id"], text) for receipt, text in native_items if receipt["id"] in chosen]
    untargeted = [
        receipt["id"]
        for receipt, _ in native_items
        if is_derived(receipt) and receipt["id"] not in chosen
    ]
    if not derived and ({MINUS_DERIVED_ARM, PLUS_DERIVED_ARM, MINUS_EVICTED_ARM} & set(arms)):
        raise ManifestError(f"the native pack for {task} carries no derived item to intervene on")
    counter = checkpoints.build_counter(tokenizer_assets)
    prompt, workspace = probe.material_task_source(tasks_root)(task, contract.POLICY_ROOT)
    references, _validate = checkpoints.summary_library()

    def recomposed(arm: str, items: list[tuple[str, str]], intervention: dict[str, Any]) -> dict:
        composed = compose(header, items, counter=counter, prompt=prompt, workspace=workspace)
        return {
            "schema": SCHEMA,
            "checkpoint": checkpoint,
            "task": task,
            "arm": arm,
            "status": composed["status"],
            "reason": None if composed["status"] == "prepared" else "oversized_first_item",
            "memory": composed["memory"],
            "counts": composed["counts"],
            "intervention": {
                **intervention,
                "kept": composed["kept"],
                "dropped": composed["dropped"],
            },
        }

    provenance = {
        arm: {
            "receipt_path": by_cell[(task, arm)]["receipt_path"],
            "pack_receipt_sha256": contract.digest(packs[arm]),
        }
        for arm in SOURCE_ARMS
    }
    documents = {
        NATIVE_ARM: {**packs[NATIVE_ARM], "intervention": {"source": provenance[NATIVE_ARM]}},
        RAW_ARM: {**packs[RAW_ARM], "intervention": {"source": provenance[RAW_ARM]}},
    }
    if MINUS_DERIVED_ARM in arms:
        documents[MINUS_DERIVED_ARM] = recomposed(
            MINUS_DERIVED_ARM,
            [
                (receipt["id"], text)
                for receipt, text in native_items
                if receipt["id"] not in chosen
            ],
            {"source": provenance[NATIVE_ARM], "removed": [item_id for item_id, _ in derived]},
        )
    if {PLUS_DERIVED_ARM, MINUS_EVICTED_ARM} & set(arms):
        plus = recomposed(
            PLUS_DERIVED_ARM,
            [*derived, *raw_items],
            {
                "source": provenance[RAW_ARM],
                "added_from": provenance[NATIVE_ARM],
                "added": [item_id for item_id, _ in derived],
            },
        )
        if PLUS_DERIVED_ARM in arms:
            documents[PLUS_DERIVED_ARM] = plus
        evicted = [i for i in plus["intervention"]["dropped"] if i not in chosen]
        documents[MINUS_EVICTED_ARM] = recomposed(
            MINUS_EVICTED_ARM,
            [(item_id, text) for item_id, text in raw_items if item_id not in evicted],
            {"source": provenance[RAW_ARM], "removed": evicted, "matches": PLUS_DERIVED_ARM},
        )
    if PLUS_SUMMARY_ARM in arms:
        block = summary_block(references, family)
        documents[PLUS_SUMMARY_ARM] = recomposed(
            PLUS_SUMMARY_ARM,
            [(family, block), *raw_items],
            {"source": provenance[RAW_ARM], "added": [family]},
        )
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    cells = [_cell(output, documents[arm]) for arm in arms]
    preparation = {
        "schema": SCHEMA,
        "checkpoint": checkpoint,
        "tasks": [task],
        "arms": list(arms),
        "families": {task: source["families"][task]},
        "summary_family": family,
        "source_preparation": {
            "path": str(preparation_root / probe.PREPARATION_NAME),
            "sha256": contract.digest(source),
            "catalog_sha256": source.get("catalog_sha256"),
            "native_inventory": source.get("native_inventory"),
        },
        "targets": [item_id for item_id, _ in derived],
        "targets_named": targets is not None,
        "untargeted_derived_items": untargeted,
        "denominator": len(arms),
        "prepared": sum(cell["status"] == "prepared" for cell in cells),
        "cells": cells,
        "memory_established": False,
        "learning_benefit_established": False,
    }
    checkpoints._write_json(output / probe.PREPARATION_NAME, preparation)
    return preparation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--arms", nargs="+", default=list(DIAGNOSTIC_ARMS))
    # A native item key, repeatable. Without one, every derived item is targeted.
    parser.add_argument("--target", action="append", default=None)
    parser.add_argument("--tasks-root", type=Path, default=probe.MATERIAL_ROOT)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--tokenizer-assets", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=probe.DEFAULT_REPETITIONS)
    parser.add_argument("--workers", type=int, default=probe.DEFAULT_WORKERS)
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    template = strict_json(args.template.read_bytes())
    output = Path(args.output).absolute()
    if output.exists() or output.is_symlink():
        raise ManifestError("intervention output already exists")
    preparation = build(
        preparation_root=Path(args.preparation).absolute(),
        output=output,
        task=args.task,
        arms=list(args.arms),
        family=args.family,
        tokenizer_assets=Path(args.tokenizer_assets),
        tasks_root=args.tasks_root,
        targets=args.target,
    )
    if args.build_only:
        sys.stdout.write(
            json.dumps(
                {"prepared": preparation["prepared"], "cells": preparation["cells"]}, indent=2
            )
            + "\n"
        )
        return 0 if preparation["prepared"] == preparation["denominator"] else probe.EXIT_NO_PACKS
    report = probe.run_cells(
        preparation=preparation,
        template=template,
        tasks_root=args.tasks_root,
        output=output,
        repetitions=args.repetitions,
        workers=args.workers,
        api_key_env=args.api_key_env,
    )
    sys.stdout.write(probe.table(report))
    return 0 if preparation["prepared"] == preparation["denominator"] else probe.EXIT_NO_PACKS


if __name__ == "__main__":
    raise SystemExit(main())
