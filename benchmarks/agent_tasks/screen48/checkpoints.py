"""Prepare one checkpoint: the native inventory and its twenty-four packs.

This module owns no memory logic and no ranking. It qualifies the retained
originals through the existing restore owners, snapshots the native universe
once per checkpoint, and then walks the six tasks by four arms, asking
``RecallAdapter.prepare`` for each cell and writing exactly what it returns.
A cell whose preparation failed stays ``missing_pack`` with its own reason;
nothing here repairs, retries or invents a pack.

Three bindings belong to the eval host rather than to this repository, and all
three are read, never guessed:

``SCREEN48_HOST_BINDING``
    A JSON file naming the signed cohort archive (``archive.path`` and
    ``archive.sha256``), the restore ``source`` row that ``qualify_originals``
    requalifies against, and the ``owners`` arguments of ``CurrentOwners``
    (source tree, manifest, manifest digest, base commit, private dependency
    runtime and the owned database URL).
``SCREEN48_OWNER_API_KEY``
    The already-issued owner key for the study organization. It is
    authenticated through the product's own API-key path to produce the intake
    context ``CurrentAuthority`` refreshes; this lane never mints authority.
``--tokenizer-assets``
    The Qwen ``tokenizer.json`` / ``tokenizer_config.json`` pair, which
    ``QwenRequestCounter`` hash-binds on construction and again on ``verify()``.

The reader is the study principal with no project, ``memory_scope='private'``
and no scope key: that is exactly what the 233 captures carry, because the
restore qualifier admits a capture only when its scope is private and its
principal is the study owner. The choice is recorded in every receipt.

Outputs, all under ``--output``:

``checkpoint.json``
    Created exclusively and sealed at the end, whatever happened.
``catalog.json``
    The 233 qualified original IDs and the catalog digest, which must equal the
    frozen schedule's ``source_catalog_sha256``.
``native-inventory.json``
    The checkpoint's authorized native universe and its production receipt.
``packs/cp{checkpoint}/{task}/{arm}.json`` and ``{arm}.txt``
    One preparation receipt per cell, beside the exact memory bytes when the
    cell was prepared. ``materialize`` reads precisely this layout.

At checkpoint 1 the two reusing arms are handed their checkpoint-0 pack, and the
digest that travels with it is the one ``--prior-root``'s own sealed
``checkpoint.json`` recorded for that cell, never a digest recomputed from the
file just read. A prior whose bytes no longer hash to what checkpoint 0 sealed
is therefore unbound rather than trusted, and the cell stays ``missing_pack``.
"""

# Product-facing imports stay inside functions: run_phase stamps SIBYL_* into
# the environment before the first sibyl import.
# ruff: noqa: PLC0415

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks.agent_tasks.screen48 import contract, cycle
from benchmarks.agent_tasks.screen48.recall.native_inventory import NativeCheckpoints
from benchmarks.agent_tasks.screen48.recall.qualification import (
    CurrentAuthority,
    CurrentOwners,
    qualify_originals,
)
from benchmarks.agent_tasks.screen48.recall.recall_adapter import Reader, RecallAdapter
from benchmarks.agent_tasks.screen48.recall.request_count import QwenRequestCounter
from benchmarks.agent_tasks.screen48.recall.whole_items import MissingPack

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

SCHEMA = "sibyl-screen48-checkpoint-preparation-v1"
CATALOG_SCHEMA = "sibyl-screen48-checkpoint-catalog-v1"
INVENTORY_SCHEMA = "sibyl-screen48-checkpoint-native-inventory-v1"

RECEIPT_NAME = "checkpoint.json"
CATALOG_NAME = "catalog.json"
INVENTORY_NAME = "native-inventory.json"
PACKS_DIRNAME = "packs"

SCHEDULE_PATH = Path(__file__).parent / "schedule" / "schedule.json"
SUMMARY_PROVENANCE = contract.MATERIAL_ROOT / "summary-library.provenance.json"

HOST_BINDING_ENV = "SCREEN48_HOST_BINDING"
OWNER_API_KEY_ENV = "SCREEN48_OWNER_API_KEY"
HOST_BINDING_KEYS = frozenset({"archive", "source", "owners"})
OWNER_KEYS = frozenset(
    {
        "source",
        "source_manifest",
        "source_manifest_sha256",
        "source_commit",
        "dependency_runtime",
        "owned_database_url",
    }
)

NATIVE_ARM = "native"
SUMMARY_ARM = "strong_summary"
NO_MEMORY_ARM = "no_memory"
#: The two arms whose checkpoint-1 pack is the separately hash-bound cp0 pack.
CP1_PRIOR_ARMS = frozenset({"raw_retrieval", SUMMARY_ARM})

#: Every native item at checkpoint 0 must be a retained raw original.
RAW_ORIGINAL_KIND = "raw_capture"
#: The inventory's own snapshot counts for everything consolidation derives.
DERIVED_SOURCE_COUNTS = ("entity", "episode", "relationship")

CELLS_PER_CHECKPOINT = len(contract.TASKS) * len(contract.ARMS)

#: The reader every graph and content read in this lane runs as.
READER_PROJECT: str | None = None
READER_MEMORY_SCOPE = "private"
READER_SCOPE_KEY: str | None = None
READER_SCOPE_EVIDENCE = (
    "The restore qualifier admits a capture only when its memory_scope is "
    "'private' and its principal is the study owner (recall/qualification.py), "
    "so the reader is that principal with no project and no scope key."
)

STATUS_PREPARED = "prepared_all_cells"
STATUS_PARTIAL = "partial_preparation"
STATUS_DERIVED_AT_ZERO = "native_inventory_not_raw_original_only"
STATUS_CATALOG_DISAGREES = "source_catalog_disagrees_with_schedule"
STATUS_ERROR = "error"
STATUS_RUNNING = "running"

#: A prior pack the sealed checkpoint-0 receipt does not vouch for.
PRIOR_UNBOUND = "unbound"

EXIT_OK = 0
EXIT_INCOMPLETE = 2


class CheckpointError(RuntimeError):
    """The checkpoint could not be prepared through the qualified owners."""


# --------------------------------------------------------------------------
# Host bindings
# --------------------------------------------------------------------------


def schedule_catalog_sha256(path: Path = SCHEDULE_PATH) -> str:
    """Read the frozen schedule's source catalog digest."""
    schedule = json.loads(path.read_bytes())
    expected = schedule.get("source_catalog_sha256")
    if not isinstance(expected, str) or not expected:
        raise CheckpointError(f"the frozen schedule names no source catalog: {path}")
    return expected


def host_binding(path: Path | str | None = None) -> dict[str, Any]:
    """Read the eval host's binding file; every location in it is the host's."""
    location = Path(path) if path is not None else Path(os.environ.get(HOST_BINDING_ENV, ""))
    if not str(location) or str(location) == ".":
        raise CheckpointError(f"{HOST_BINDING_ENV} must name the screen48 host binding file")
    binding = json.loads(location.read_bytes())
    if not isinstance(binding, dict) or set(binding) != HOST_BINDING_KEYS:
        raise CheckpointError(f"host binding keys must be {sorted(HOST_BINDING_KEYS)}: {location}")
    if set(binding["owners"]) != OWNER_KEYS:
        raise CheckpointError(f"host binding owner keys must be {sorted(OWNER_KEYS)}: {location}")
    if set(binding["archive"]) != {"path", "sha256"}:
        raise CheckpointError(f"host binding archive needs a path and a sha256: {location}")
    return binding


async def authenticated_intake(group_id: str, principal_id: str):
    """Authenticate the host's owner key through the product's own API-key path."""
    from sibyl.auth.dependencies import _api_key_allows_rest, _api_key_claims
    from sibyl.persistence.auth_runtime import authenticate_api_key, resolve_auth_context

    raw_key = os.environ.get(OWNER_API_KEY_ENV, "")
    if not raw_key:
        raise CheckpointError(f"{OWNER_API_KEY_ENV} must carry the study organization owner key")
    auth = await authenticate_api_key(raw_key)
    if auth is None:
        raise CheckpointError("the configured owner key did not authenticate")
    scopes = list(auth.scopes or [])
    if not _api_key_allows_rest(scopes=scopes, method="GET"):
        raise CheckpointError("the configured owner key cannot read through REST")
    context = await resolve_auth_context(claims=_api_key_claims(auth, scopes=scopes))
    if (context.organization_id, context.user_id) != (group_id, principal_id):
        raise CheckpointError("the configured owner key belongs to another organization or user")
    return context


async def qualify_catalog(*, group_id: str, principal_id: str) -> tuple[Any, dict, Any, Any]:
    """Requalify the 233 retained originals and return the owners that did it."""
    from benchmarks.agent_tasks.screen48.recall.owners.cohort_authority import archive_material

    binding = host_binding()
    source = dict(binding["source"])
    if (source.get("organization_id"), source.get("principal_id")) != (group_id, principal_id):
        raise CheckpointError("the host binding names another organization or principal")
    verify_owners = CurrentOwners(**binding["owners"])
    resolve_authority = CurrentAuthority(await authenticated_intake(group_id, principal_id))
    material = archive_material(Path(binding["archive"]["path"]), binding["archive"]["sha256"])
    catalog, receipt = await qualify_originals(
        material=material,
        source=source,
        resolve_authority=resolve_authority,
        verify_owners=verify_owners,
    )
    return catalog, receipt, resolve_authority, verify_owners


def build_counter(tokenizer_assets: Path):
    """The tokenizer-backed counter the study's request counts are measured with."""
    counter = QwenRequestCounter(assets=Path(tokenizer_assets))
    counter.verify()
    return counter


def summary_library(
    library_path: Path = contract.SUMMARY_LIBRARY,
    provenance_path: Path = SUMMARY_PROVENANCE,
) -> tuple[dict, Callable[[dict, dict], dict]]:
    """Load the accepted reference library and a validator that only re-binds it.

    The library is construction output, lifted verbatim from a run this lane
    does not own. The validator therefore checks lineage and nothing else: the
    vendored bytes still hash to the provenance digest, and the references
    handed to ``summary_items`` are still exactly those bytes.
    """
    provenance = json.loads(provenance_path.read_bytes())
    raw = library_path.read_bytes()
    if contract.sha(raw) != provenance["summary_library_sha256"]:
        raise CheckpointError(
            "the vendored summary library is not the accepted construction output"
        )
    references = json.loads(raw)
    sources = {sid for ref in references.values() for sid in ref["source_ids"]}
    if len(references) != provenance["references"] or len(sources) != provenance["source_ids"]:
        raise CheckpointError("the summary library does not cover the accepted denominators")
    accepted = contract.digest(references)

    def validate(candidate: dict, catalog_receipt: dict) -> dict:
        if contract.sha(library_path.read_bytes()) != provenance["summary_library_sha256"]:
            raise MissingPack("accepted_summary_library_changed")
        if contract.digest(candidate) != accepted:
            raise MissingPack("summary_references_are_not_the_accepted_library")
        return {
            "references_sha256": accepted,
            "catalog_sha256": contract.digest(catalog_receipt),
        }

    return references, validate


# --------------------------------------------------------------------------
# Durable evidence
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _reserve(path: Path, header: dict[str, Any]) -> None:
    """Create the checkpoint receipt exclusively; a second run never overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(header, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
    except FileExistsError as exc:
        raise CheckpointError(f"{path} already exists; refusing to overwrite") from exc


def _write_pack(output: Path, document: dict[str, Any]) -> dict[str, str | None]:
    """Write one preparation receipt beside its exact memory bytes."""
    directory = output / PACKS_DIRNAME / f"cp{document['checkpoint']}" / document["task"]
    arm = document["arm"]
    _write_json(directory / f"{arm}.json", document)
    memory_path: Path | None = None
    if document["status"] == "prepared":
        memory_path = directory / f"{arm}.txt"
        with memory_path.open("wb") as handle:
            handle.write(document["memory"].encode())
            handle.flush()
            os.fsync(handle.fileno())
    return {
        "receipt_path": str((directory / f"{arm}.json").relative_to(output)),
        "memory_path": str(memory_path.relative_to(output)) if memory_path else None,
    }


# --------------------------------------------------------------------------
# Cell policy
# --------------------------------------------------------------------------


def coordinate(pack: dict[str, Any], *, checkpoint: int, task: str, arm: str, catalog: str) -> dict:
    """Stamp the four coordinates ``materialize`` reads, refusing a receipt that differs."""
    coordinates = {
        "checkpoint": checkpoint,
        "task": task,
        "arm": arm,
        "catalog_sha256": catalog,
    }
    for key, value in coordinates.items():
        # A receipt that names no coordinate is not a receipt for this cell:
        # stamping one would invent the pack this module promises never to invent.
        if key not in pack or pack[key] != value:
            raise CheckpointError(f"preparation receipt disagrees on {key}: cp{checkpoint}/{task}")
    document = {**pack, **coordinates}
    if document.get("status") not in {"prepared", "missing_pack"}:
        raise CheckpointError(f"unknown preparation status: cp{checkpoint}/{task}/{arm}")
    if document["status"] == "missing_pack":
        if document.get("memory") is not None:
            raise CheckpointError(f"unprepared pack carries memory: cp{checkpoint}/{task}/{arm}")
        if not document.get("reason"):
            raise CheckpointError(f"unprepared pack carries no reason: cp{checkpoint}/{task}/{arm}")
    elif not isinstance(document.get("memory"), str):
        raise CheckpointError(f"prepared pack has no memory text: cp{checkpoint}/{task}/{arm}")
    elif arm == NO_MEMORY_ARM and document["memory"] != "":
        raise CheckpointError(f"the no-memory arm carries memory: cp{checkpoint}/{task}")
    return document


def prior_bindings(
    prior_root: Path | None, *, catalog_sha256: str
) -> tuple[dict[tuple[str, str], str], str | None]:
    """Read the prior run's sealed receipt: the only authority on its own packs.

    Returns the per-cell pack digests checkpoint 0 recorded, or the reason this
    prior root binds nothing at all. Recomputing a digest from the pack file
    would bind the file to itself and vouch for any rewrite of it.
    """
    if prior_root is None:
        return {}, None
    path = Path(prior_root) / RECEIPT_NAME
    if path.is_symlink() or not path.is_file():
        return {}, "prior_checkpoint_receipt_absent"
    try:
        sealed: Any = json.loads(path.read_bytes())
    except ValueError:
        sealed = None
    reason: str | None = None
    if not isinstance(sealed, dict):
        reason = "prior_checkpoint_receipt_unreadable"
    elif sealed.get("checkpoint") != 0:
        reason = "prior_checkpoint_receipt_is_not_checkpoint_zero"
    elif sealed.get("catalog_sha256") != catalog_sha256:
        reason = "prior_checkpoint_receipt_catalog_disagrees"
    if reason is not None:
        return {}, reason
    bindings: dict[tuple[str, str], str] = {}
    for row in sealed.get("cells") or []:
        sha256 = row.get("pack_receipt_sha256") if isinstance(row, dict) else None
        if isinstance(sha256, str) and sha256:
            bindings[(str(row.get("task")), str(row.get("arm")))] = sha256
    return bindings, None


def load_prior(
    prior_root: Path | None,
    task: str,
    arm: str,
    *,
    bound_sha256: str | None = None,
    unbound_reason: str | None = None,
) -> tuple[dict | None, dict]:
    """Read one checkpoint-0 receipt; an absent prior stays absent, never invented."""
    if prior_root is None:
        return None, {"status": "no_prior_root", "path": None, "sha256": None}
    path = Path(prior_root) / PACKS_DIRNAME / "cp0" / task / f"{arm}.json"
    record = {"status": "absent", "path": str(path), "sha256": None}
    if unbound_reason is not None:
        return None, {**record, "status": PRIOR_UNBOUND, "reason": unbound_reason}
    if path.is_symlink() or not path.is_file():
        return None, record
    try:
        prior: Any = json.loads(path.read_bytes())
    except ValueError:
        prior = None
    if not isinstance(prior, dict):
        return None, {**record, "status": "unreadable"}
    record = {**record, "sha256": contract.digest(prior)}
    if bound_sha256 is None:
        reason = "prior_checkpoint_receipt_names_no_such_cell"
    elif record["sha256"] != bound_sha256:
        reason = "prior_receipt_digest_mismatch"
    else:
        return prior, {**record, "status": "loaded"}
    return None, {**record, "status": PRIOR_UNBOUND, "reason": reason}


def inventory_shape(items: dict, receipt: dict) -> dict[str, Any]:
    """Summarize one native inventory, separating raw originals from derived items."""
    provenance = receipt.get("provenance") or {}
    counts = receipt.get("source_counts") or {}
    kinds: dict[str, int] = {}
    derived: list[str] = []
    for key in sorted(items):
        kind = str((provenance.get(key) or {}).get("kind") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind != RAW_ORIGINAL_KIND:
            derived.append(key)
    derived_counts = {name: int(counts.get(name) or 0) for name in DERIVED_SOURCE_COUNTS}
    return {
        "path": INVENTORY_NAME,
        "sha256": contract.digest(items),
        "authorized_count": receipt.get("authorized_count"),
        "source_counts": dict(counts),
        "authorized_kinds": kinds,
        "raw_item_count": kinds.get(RAW_ORIGINAL_KIND, 0),
        "derived_source_counts": derived_counts,
        "derived_items": derived,
        "raw_original_only": not derived and not any(derived_counts.values()),
        "excluded": receipt.get("excluded"),
    }


# --------------------------------------------------------------------------
# Preparation
# --------------------------------------------------------------------------


async def _prepare_cells(
    receipt: dict[str, Any],
    *,
    adapter: RecallAdapter,
    checkpoint: int,
    output: Path,
    prior_root: Path | None,
    native_inventory: dict,
    references: dict,
    catalog_sha256: str,
) -> None:
    """Walk the twenty-four cells of one checkpoint, writing exactly what came back."""
    cells: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    prepared = 0
    bindings: dict[tuple[str, str], str] = {}
    unbound: str | None = None
    if checkpoint == 1:
        bindings, unbound = prior_bindings(prior_root, catalog_sha256=catalog_sha256)
        receipt["prior_bindings"] = {
            "root": str(prior_root) if prior_root is not None else None,
            "bound_cells": len(bindings),
            "unbound_reason": unbound,
        }
    for task in contract.TASKS:
        for arm in contract.ARMS:
            arguments: dict[str, Any] = {}
            prior_record: dict[str, Any] | None = None
            if arm == NATIVE_ARM:
                arguments["native_inventory"] = native_inventory
            if arm == SUMMARY_ARM:
                arguments["references"] = references
            if checkpoint == 1 and arm in CP1_PRIOR_ARMS:
                bound_sha256 = bindings.get((task, arm))
                prior, prior_record = load_prior(
                    prior_root, task, arm, bound_sha256=bound_sha256, unbound_reason=unbound
                )
                if prior is not None:
                    arguments["prior"] = prior
                    # The digest the prior run sealed, so the adapter's own
                    # binding check can actually fail on a rewritten pack.
                    arguments["prior_sha256"] = bound_sha256
            pack = await adapter.prepare(checkpoint=checkpoint, task=task, arm=arm, **arguments)
            document = coordinate(
                pack, checkpoint=checkpoint, task=task, arm=arm, catalog=catalog_sha256
            )
            paths = _write_pack(output, document)
            counts = document.get("counts") or {}
            memory = document["memory"] if document["status"] == "prepared" else None
            if memory is None:
                reason = str(document.get("reason") or "not_prepared")
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                prepared += 1
            cells.append(
                {
                    "checkpoint": checkpoint,
                    "task": task,
                    "arm": arm,
                    "status": document["status"],
                    "reason": document.get("reason"),
                    "pack_receipt_sha256": contract.digest(document),
                    "memory_sha256": contract.sha(memory.encode()) if memory is not None else None,
                    "memory_bytes": len(memory.encode()) if memory is not None else None,
                    "memory_tokens": counts.get("memory_tokens"),
                    "fits": counts.get("fits"),
                    "prior": prior_record,
                    **paths,
                }
            )
    receipt["cells"] = cells
    receipt["prepared"] = prepared
    receipt["missing_reasons"] = dict(sorted(reasons.items()))
    receipt["status"] = STATUS_PREPARED if prepared == CELLS_PER_CHECKPOINT else STATUS_PARTIAL


async def _prepare(
    receipt: dict[str, Any],
    *,
    checkpoint: int,
    output: Path,
    tokenizer_assets: Path,
    prior_root: Path | None,
    group_id: str,
    principal_id: str,
    reader: Reader,
) -> None:
    """Qualify the catalog, snapshot the native universe, then prepare the cells."""
    catalog, qualification, resolve_authority, verify_owners = await qualify_catalog(
        group_id=group_id, principal_id=principal_id
    )
    receipt["catalog_sha256"] = catalog.catalog_sha256
    receipt["catalog"] = {"path": CATALOG_NAME, "source_count": len(catalog.rows)}
    _write_json(
        output / CATALOG_NAME,
        {
            "schema": CATALOG_SCHEMA,
            "checkpoint": checkpoint,
            "organization_id": catalog.organization_id,
            "catalog_sha256": catalog.catalog_sha256,
            "schedule_source_catalog_sha256": receipt["schedule_source_catalog_sha256"],
            "source_count": len(catalog.rows),
            "source_ids": sorted(catalog.rows),
            "qualification": qualification,
        },
    )
    if len(catalog.rows) != contract.SOURCE_COUNT:
        raise CheckpointError(f"the qualified catalog holds {len(catalog.rows)} originals")
    if catalog.catalog_sha256 != receipt["schedule_source_catalog_sha256"]:
        receipt["status"] = STATUS_CATALOG_DISAGREES
        raise CheckpointError(
            "the qualified catalog is not the schedule's: the frozen material and the "
            f"database disagree ({catalog.catalog_sha256})"
        )

    natives = NativeCheckpoints(
        catalog=catalog,
        reader=reader,
        resolve_authority=resolve_authority,
        verify_owners=verify_owners,
    )
    items, inventory = await natives.produce(checkpoint)
    shape = inventory_shape(items, inventory)
    receipt["native_inventory"] = shape
    _write_json(
        output / INVENTORY_NAME,
        {
            "schema": INVENTORY_SCHEMA,
            "checkpoint": checkpoint,
            "items_sha256": shape["sha256"],
            "receipt": inventory,
            "items": items,
        },
    )
    if checkpoint == 0 and not shape["raw_original_only"]:
        receipt["status"] = STATUS_DERIVED_AT_ZERO
        raise CheckpointError(
            "checkpoint 0 is the state before one consolidation cycle, so the native "
            "inventory must hold retained raw originals only; this organization already "
            f"holds derived items (snapshot {shape['derived_source_counts']}, "
            f"{len(shape['derived_items'])} authorized non-raw items)"
        )

    counter = build_counter(tokenizer_assets)
    receipt["tokenizer"] = {
        **counter.tokenizer_receipt(),
        "assets": str(tokenizer_assets),
        "verified": True,
    }
    references, validate_summary_library = summary_library()
    adapter = RecallAdapter(
        catalog=catalog,
        reader=reader,
        counter=counter,
        resolve_authority=resolve_authority,
        verify_owners=verify_owners,
        verify_native_inventory=natives.verify,
        validate_summary_library=validate_summary_library,
    )
    await _prepare_cells(
        receipt,
        adapter=adapter,
        checkpoint=checkpoint,
        output=output,
        prior_root=prior_root,
        native_inventory=items,
        references=references,
        catalog_sha256=catalog.catalog_sha256,
    )


async def prepare_checkpoint(
    checkpoint: int,
    *,
    output: Path,
    tokenizer_assets: Path,
    prior_root: Path | None,
    group_id: str = contract.ORGANIZATION_ID,
    principal_id: str = contract.PRINCIPAL_ID,
) -> dict:
    """Prepare one checkpoint's inventory and packs, sealing a durable receipt."""
    if type(checkpoint) is not int or checkpoint not in contract.CHECKPOINTS:
        raise CheckpointError(f"unknown checkpoint: {checkpoint!r}")
    # absolute() joins against the working directory; it reads no filesystem.
    output = Path(output).absolute()  # noqa: ASYNC240
    reader = Reader(principal_id, READER_PROJECT, READER_MEMORY_SCOPE, READER_SCOPE_KEY)
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": STATUS_RUNNING,
        "checkpoint": checkpoint,
        "group_id": group_id,
        "principal": principal_id,
        "reader": asdict(reader),
        "reader_scope_evidence": READER_SCOPE_EVIDENCE,
        "output": str(output),
        "prior_root": str(prior_root) if prior_root is not None else None,
        "tokenizer_assets": str(tokenizer_assets),
        # Read inside the try below: a failure there must still leave a receipt.
        "schedule_source_catalog_sha256": None,
        "catalog_sha256": None,
        "catalog": None,
        "native_inventory": None,
        "tokenizer": None,
        "prior_bindings": None,
        "denominator": CELLS_PER_CHECKPOINT,
        "prepared": 0,
        "cells": [],
        "missing_reasons": {},
        "errors": [],
        "solver_calls": 0,
        "reservation_changes": 0,
        "learning_claim": False,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
    }
    _reserve(output / RECEIPT_NAME, receipt)
    try:
        receipt["schedule_source_catalog_sha256"] = schedule_catalog_sha256()
        await cycle.bootstrap_runtime()
        try:
            await _prepare(
                receipt,
                checkpoint=checkpoint,
                output=output,
                tokenizer_assets=Path(tokenizer_assets),
                prior_root=Path(prior_root) if prior_root is not None else None,
                group_id=group_id,
                principal_id=principal_id,
                reader=reader,
            )
        finally:
            try:
                await cycle.shutdown_runtime()
            except Exception as exc:  # a drain failure must not eat the receipt
                receipt["errors"].append(f"shutdown:{type(exc).__name__}: {exc}")
    except Exception as exc:
        receipt["errors"].append(f"{type(exc).__name__}: {exc}")
        if receipt["status"] in {STATUS_RUNNING, STATUS_PREPARED, STATUS_PARTIAL}:
            receipt["status"] = STATUS_ERROR
        raise
    finally:
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        _write_json(output / RECEIPT_NAME, receipt)
    return receipt


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen48-checkpoint", description=__doc__)
    parser.add_argument("--checkpoint", type=int, required=True, choices=list(contract.CHECKPOINTS))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tokenizer-assets", type=Path, required=True)
    parser.add_argument(
        "--prior-root",
        type=Path,
        default=None,
        help="the checkpoint-0 output directory, required at checkpoint 1",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    import asyncio

    args = build_parser().parse_args(argv)
    try:
        receipt = asyncio.run(
            prepare_checkpoint(
                args.checkpoint,
                output=args.output,
                tokenizer_assets=args.tokenizer_assets,
                prior_root=args.prior_root,
            )
        )
    except (CheckpointError, MissingPack, OSError) as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_INCOMPLETE
    except Exception as exc:  # the receipt is already sealed; never exit on a traceback
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return EXIT_INCOMPLETE
    sys.stdout.write(
        json.dumps(
            {
                "status": receipt["status"],
                "checkpoint": receipt["checkpoint"],
                "prepared": receipt["prepared"],
                "denominator": receipt["denominator"],
                "missing_reasons": receipt["missing_reasons"],
                "errors": receipt["errors"],
                "catalog_sha256": receipt["catalog_sha256"],
                "receipt": str(Path(receipt["output"]) / RECEIPT_NAME),
            },
            sort_keys=True,
        )
        + "\n"
    )
    # An error recorded after the last cell (a queue that would not drain) is
    # still an incomplete checkpoint, whatever the cell count says.
    if receipt["status"] != STATUS_PREPARED or receipt["errors"]:
        return EXIT_INCOMPLETE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
