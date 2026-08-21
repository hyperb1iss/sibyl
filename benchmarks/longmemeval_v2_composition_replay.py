#!/usr/bin/env python3
"""Replay LongMemEval-V2 evidence composition from sealed run artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import string
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.longmemeval_v2_memory.sibyl_memory import (
    DEFAULT_CONTEXT_CHARS_PER_ITEM,
    DEFAULT_CONTEXT_TOTAL_CHARS,
    assemble_context_results,
    compile_operational_evidence_set,
    reader_char_total_activity,
    render_memory_context,
)
from benchmarks.longmemeval_v2_memory.render_bundle import (
    ACTION_SPINE_FILENAME,
    DISTILLATION_RECEIPT_FILENAME,
    LEVER_ACTION_SPINES,
    LEVER_CONTEXT_TOTAL_CHARS,
    LEVER_ENGLISH_LANE_GROUPING,
    RENDER_BUNDLE_LEVERS,
    append_action_spines,
    canonical_sha256,
    file_sha256,
    group_results_by_lane,
    read_action_spines,
    read_distillation_receipts,
    screen_context_composition_receipt,
    screen_distillation_receipts,
)

SCHEMA_VERSION = "sibyl-longmemeval-v2-composition-replay-v1"
DEFAULT_RENDER_TREATMENT_TOTAL_CHARS = 400_000
_PHRASE_SET_EVALUATOR = "norm_phrase_set_match"
_LEGACY_CONTEXT_HEADER = re.compile(
    r"^Retrieved evidence rank (?P<rank>\d+)\n"
    r"Trajectory: (?P<trajectory>[^\n]+)\n"
    r"Chunk: (?P<chunk>\d+)\n"
    r"Score: (?P<score>[^\n]+)$"
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runs = parse_specs(args.run)
    catalogs = parse_specs(args.chunk_catalog)
    manifests = parse_specs(args.memory_manifest)
    if runs.keys() != catalogs.keys() or runs.keys() != manifests.keys():
        raise ValueError("--run, --chunk-catalog, and --memory-manifest domains must match")
    report = replay_composition(
        runs=runs,
        catalogs=catalogs,
        manifests=manifests,
        max_items=args.max_items,
        max_chunks_per_trajectory=args.max_chunks_per_trajectory,
        neighbor_stitch_items=args.neighbor_stitch_items,
        neighbor_stitch_span=args.neighbor_stitch_span,
        state_part_completion_items=args.state_part_completion_items,
        state_part_refinement=args.state_part_refinement,
        neighbor_support_exempt=args.neighbor_support_exempt,
        neighbor_trajectory_preserving=args.neighbor_trajectory_preserving,
        neighbor_support_overflow_items=args.neighbor_support_overflow_items,
        neighbor_stitch_spread=args.neighbor_stitch_spread,
        render_max_total_chars=args.render_max_total_chars,
        render_max_chars_per_item=args.render_max_chars_per_item,
        replay_cost_budget_usd=args.replay_cost_budget_usd,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(  # noqa: T201
        json.dumps(
            {
                "schema_version": report["schema_version"],
                "sources": report["sources"],
                "configuration": report["configuration"],
                "metrics": report["metrics"],
                "gate": report["gate"],
                "render_screen": report["render_screen"],
                "replay_survivors": report["replay_survivors"],
                "bundle_eligible": report["bundle_eligible"],
                "cost": report["cost"],
                "gained_question_ids": report["gained_question_ids"],
                "lost_question_ids": report["lost_question_ids"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, metavar="DOMAIN=PER_QUESTION")
    parser.add_argument(
        "--chunk-catalog",
        action="append",
        required=True,
        metavar="DOMAIN=CHUNK_CATALOG",
    )
    parser.add_argument(
        "--memory-manifest",
        action="append",
        required=True,
        metavar="DOMAIN=MEMORY_MANIFEST",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-items", type=int, default=8)
    parser.add_argument("--max-chunks-per-trajectory", type=int, default=2)
    parser.add_argument("--neighbor-stitch-items", type=int, default=2)
    parser.add_argument("--neighbor-stitch-span", type=int, default=1)
    parser.add_argument("--state-part-completion-items", type=int, default=0)
    parser.add_argument("--state-part-refinement", action="store_true")
    parser.add_argument("--neighbor-support-exempt", action="store_true")
    parser.add_argument("--neighbor-trajectory-preserving", action="store_true")
    parser.add_argument("--neighbor-support-overflow-items", type=int, default=0)
    parser.add_argument("--neighbor-stitch-spread", action="store_true")
    parser.add_argument(
        "--render-max-total-chars",
        type=int,
        default=DEFAULT_RENDER_TREATMENT_TOTAL_CHARS,
    )
    parser.add_argument(
        "--render-max-chars-per-item",
        type=int,
        default=DEFAULT_CONTEXT_CHARS_PER_ITEM,
    )
    parser.add_argument("--replay-cost-budget-usd", type=float, default=0.0)
    args = parser.parse_args(argv)
    for name in (
        "max_items",
        "max_chunks_per_trajectory",
        "neighbor_stitch_items",
        "neighbor_stitch_span",
        "state_part_completion_items",
        "render_max_total_chars",
        "render_max_chars_per_item",
    ):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} cannot be negative")
    if args.max_items < 1 or args.max_chunks_per_trajectory < 1:
        parser.error("item and per-trajectory limits must be positive")
    if args.render_max_total_chars <= DEFAULT_CONTEXT_TOTAL_CHARS:
        parser.error(
            "--render-max-total-chars must exceed the frozen 60000-character control"
        )
    if args.replay_cost_budget_usd < 0:
        parser.error("--replay-cost-budget-usd cannot be negative")
    return args


def parse_specs(raw_specs: list[str]) -> dict[str, Path]:
    specs: dict[str, Path] = {}
    for raw_spec in raw_specs:
        domain, separator, raw_path = raw_spec.partition("=")
        domain = domain.strip()
        if not separator or not domain or not raw_path.strip():
            raise ValueError(f"Invalid domain path specification: {raw_spec!r}")
        if domain in specs:
            raise ValueError(f"Duplicate domain specification: {domain!r}")
        specs[domain] = Path(raw_path).expanduser().resolve()
    return specs


def replay_composition(
    *,
    runs: dict[str, Path],
    catalogs: dict[str, Path],
    manifests: dict[str, Path],
    max_items: int,
    max_chunks_per_trajectory: int,
    neighbor_stitch_items: int,
    neighbor_stitch_span: int,
    state_part_completion_items: int = 0,
    state_part_refinement: bool = False,
    neighbor_support_exempt: bool = False,
    neighbor_trajectory_preserving: bool = False,
    neighbor_support_overflow_items: int = 0,
    neighbor_stitch_spread: bool = False,
    render_max_total_chars: int = DEFAULT_RENDER_TREATMENT_TOTAL_CHARS,
    render_max_chars_per_item: int = DEFAULT_CONTEXT_CHARS_PER_ITEM,
    replay_cost_budget_usd: float = 0.0,
) -> dict[str, Any]:
    if render_max_total_chars <= DEFAULT_CONTEXT_TOTAL_CHARS:
        raise ValueError("render treatment total must exceed the frozen control")
    if render_max_chars_per_item < 1:
        raise ValueError("render per-item character ceiling must be positive")
    if replay_cost_budget_usd < 0:
        raise ValueError("replay cost budget cannot be negative")
    rows: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    distillation_screens_by_domain: dict[str, dict[str, dict[str, object]]] = {}
    for domain in sorted(runs):
        run_path = runs[domain]
        catalog_path = catalogs[domain]
        manifest_path = manifests[domain]
        catalog = load_chunk_catalog(catalog_path)
        domain_rows = load_jsonl(run_path)
        manifest = load_memory_manifest(manifest_path)
        validate_memory_binding(
            manifest=manifest,
            catalog_path=catalog_path,
            rows=domain_rows,
        )
        action_spines, distillation_receipts, render_source_digests = (
            load_bound_render_artifacts(
                manifest=manifest,
                manifest_path=manifest_path,
            )
        )
        validate_render_receipt_binding(
            rows=domain_rows,
            distillation_receipts=distillation_receipts,
        )
        distillation_screens_by_domain[domain] = screen_distillation_receipts(
            distillation_receipts
        )
        sources[domain] = {
            "per_question_sha256": sha256_file(run_path),
            "chunk_catalog_sha256": sha256_file(catalog_path),
            "memory_manifest_sha256": sha256_file(manifest_path),
            "question_count": len(domain_rows),
            **render_source_digests,
        }
        for row in domain_rows:
            rows.append(
                replay_question(
                    domain=domain,
                    row=row,
                    chunk_catalog=catalog,
                    max_items=max_items,
                    max_chunks_per_trajectory=max_chunks_per_trajectory,
                    neighbor_stitch_items=neighbor_stitch_items,
                    neighbor_stitch_span=neighbor_stitch_span,
                    state_part_completion_items=state_part_completion_items,
                    state_part_refinement=state_part_refinement,
                    neighbor_support_exempt=neighbor_support_exempt,
                    neighbor_trajectory_preserving=neighbor_trajectory_preserving,
                    neighbor_support_overflow_items=neighbor_support_overflow_items,
                    neighbor_stitch_spread=neighbor_stitch_spread,
                    action_spines=action_spines,
                    render_max_total_chars=render_max_total_chars,
                    render_max_chars_per_item=render_max_chars_per_item,
                )
            )

    phrase_rows = [row for row in rows if row["phrase_set_eligible"]]
    baseline_exposed = sum(bool(row["baseline_full_phrase_exposure"]) for row in phrase_rows)
    candidate_exposed = sum(bool(row["candidate_full_phrase_exposure"]) for row in phrase_rows)
    phrase_count = len(phrase_rows)
    typed_evidence_available = any(int(row["baseline_typed_count"]) for row in rows)
    baseline_rate = baseline_exposed / phrase_count if phrase_count else 0.0
    candidate_rate = candidate_exposed / phrase_count if phrase_count else 0.0
    lost = [
        row["question_id"]
        for row in phrase_rows
        if row["baseline_full_phrase_exposure"] and not row["candidate_full_phrase_exposure"]
    ]
    gained = [
        row["question_id"]
        for row in phrase_rows
        if row["candidate_full_phrase_exposure"] and not row["baseline_full_phrase_exposure"]
    ]
    configuration = {
        "max_items": max_items,
        "max_chunks_per_trajectory": max_chunks_per_trajectory,
        "neighbor_stitch_items": neighbor_stitch_items,
        "neighbor_stitch_span": neighbor_stitch_span,
        "state_part_completion_items": state_part_completion_items,
        "state_part_refinement": state_part_refinement,
        "neighbor_support_exempt": neighbor_support_exempt,
        "neighbor_trajectory_preserving": neighbor_trajectory_preserving,
        "neighbor_support_overflow_items": neighbor_support_overflow_items,
        "neighbor_stitch_spread": neighbor_stitch_spread,
        "render_control_total_chars": DEFAULT_CONTEXT_TOTAL_CHARS,
        "render_max_total_chars": render_max_total_chars,
        "render_max_chars_per_item": render_max_chars_per_item,
    }
    render_screen = aggregate_render_screens(
        rows=rows,
        distillation_screens_by_domain=distillation_screens_by_domain,
        hard_total_chars=render_max_total_chars,
    )
    replay_survivors = {
        lever: render_screen["levers"][lever]["survives"] for lever in RENDER_BUNDLE_LEVERS
    }
    source_digests = {
        "configuration_sha256": canonical_sha256(configuration),
        "domains": {
            domain: {
                key: value
                for key, value in source.items()
                if key.endswith("_sha256")
            }
            for domain, source in sources.items()
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": sources,
        "source_digests": source_digests,
        "configuration": configuration,
        "metrics": {
            "question_count": len(rows),
            "phrase_set_question_count": phrase_count,
            "baseline_full_phrase_exposure_count": baseline_exposed,
            "baseline_full_phrase_exposure_rate": baseline_rate,
            "candidate_full_phrase_exposure_count": candidate_exposed,
            "candidate_full_phrase_exposure_rate": candidate_rate,
            "full_phrase_exposure_gain_pp": (candidate_rate - baseline_rate) * 100.0,
            "questions_gaining_full_phrase_exposure": len(gained),
            "questions_losing_full_phrase_exposure": len(lost),
            "changed_context_count": sum(bool(row["context_changed"]) for row in rows),
            "raw_assembly_parity_count": sum(
                bool(row["raw_assembly_matches_baseline"]) for row in rows
            ),
            "raw_assembly_parity_rate": (
                sum(bool(row["raw_assembly_matches_baseline"]) for row in rows) / len(rows)
                if rows
                else 0.0
            ),
            "baseline_typed_item_count": sum(int(row["baseline_typed_count"]) for row in rows),
            "candidate_typed_item_count": sum(int(row["candidate_typed_count"]) for row in rows),
            "candidate_neighbor_item_count": sum(
                int(row["candidate_neighbor_count"]) for row in rows
            ),
            "candidate_state_part_item_count": sum(
                int(row["candidate_state_part_count"]) for row in rows
            ),
            "candidate_state_refinement_count": sum(
                int(row["candidate_state_refinement_count"]) for row in rows
            ),
            "typed_evidence_available": typed_evidence_available,
            "legacy_context_header_question_count": sum(
                row["artifact_trace_mode"] == "legacy_context_headers" for row in rows
            ),
            "typed_entity_type_fallback_count": sum(
                int(row["typed_entity_type_fallback_count"]) for row in rows
            ),
        },
        "gate": {
            "minimum_exposure_gain_pp": 3.0,
            "requires_zero_exposure_losses": True,
            "requires_raw_assembly_parity": True,
            "requires_typed_evidence": True,
            "pass": (
                candidate_rate - baseline_rate >= 0.03
                and not lost
                and all(row["raw_assembly_matches_baseline"] for row in rows)
                and typed_evidence_available
            ),
        },
        "gained_question_ids": gained,
        "lost_question_ids": lost,
        "render_screen": render_screen,
        "replay_survivors": replay_survivors,
        "bundle_eligible": all(replay_survivors.values()),
        "cost": {
            "budget_usd": replay_cost_budget_usd,
            "actual_usd": 0.0,
            "provider_calls": 0,
            "within_budget": 0.0 <= replay_cost_budget_usd,
        },
        "questions": rows,
    }


def replay_question(
    *,
    domain: str,
    row: dict[str, Any],
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
    max_items: int,
    max_chunks_per_trajectory: int,
    neighbor_stitch_items: int,
    neighbor_stitch_span: int,
    state_part_completion_items: int,
    state_part_refinement: bool,
    neighbor_support_exempt: bool = False,
    neighbor_trajectory_preserving: bool = False,
    neighbor_support_overflow_items: int = 0,
    neighbor_stitch_spread: bool = False,
    action_spines: dict[str, dict[str, object]] | None = None,
    render_max_total_chars: int = DEFAULT_RENDER_TREATMENT_TOTAL_CHARS,
    render_max_chars_per_item: int = DEFAULT_CONTEXT_CHARS_PER_ITEM,
) -> dict[str, Any]:
    query = str(row.get("question_text") or "")
    baseline = result_candidates(row)
    validate_seed_catalog_content(baseline, chunk_catalog=chunk_catalog)
    typed = [
        candidate
        for candidate in baseline
        if str(candidate.get("_selection_origin") or "").startswith("context_pack:")
    ]
    search_seeds = [
        candidate
        for candidate in baseline
        if candidate.get("_selection_origin") in {"search", "state_part_refinement"}
    ]
    search_seeds.sort(key=lambda item: int(item.get("_search_rank") or max_items + 1))
    baseline_assembled, _baseline_assembly = assemble_context_results(
        search_seeds,
        chunk_catalog=chunk_catalog,
        max_items=max_items,
        max_chunks_per_trajectory=max_chunks_per_trajectory,
        neighbor_stitch_items=neighbor_stitch_items,
        neighbor_stitch_span=neighbor_stitch_span,
        query=query,
    )
    candidate_assembled, candidate_assembly = assemble_context_results(
        search_seeds,
        chunk_catalog=chunk_catalog,
        max_items=max_items,
        max_chunks_per_trajectory=max_chunks_per_trajectory,
        neighbor_stitch_items=neighbor_stitch_items,
        neighbor_stitch_span=neighbor_stitch_span,
        neighbor_stitch_spread=neighbor_stitch_spread,
        query=query,
        state_part_completion_items=state_part_completion_items,
        state_part_refinement=state_part_refinement,
    )
    candidate, composition = compile_operational_evidence_set(
        query=query,
        typed_results=typed,
        raw_results=candidate_assembled,
        max_items=max_items,
        mode="shared_relevance",
        neighbor_support_exempt=neighbor_support_exempt,
        neighbor_trajectory_preserving=neighbor_trajectory_preserving,
        neighbor_support_overflow_items=neighbor_support_overflow_items,
    )
    phrases = answer_phrases(row)
    baseline_exposed = full_phrase_exposure(phrases, baseline)
    candidate_exposed = full_phrase_exposure(phrases, candidate)
    baseline_keys = [candidate_key(item) for item in baseline]
    candidate_keys = [candidate_key(item) for item in candidate]
    baseline_raw_keys = [
        candidate_key(item)
        for item in baseline
        if not str(item.get("_selection_origin") or "").startswith("context_pack:")
    ]
    assembled_keys = [candidate_key(item) for item in baseline_assembled]
    render_screens = replay_question_render_levers(
        row=row,
        candidates=candidate,
        action_spines=action_spines or {},
        max_items=max_items,
        query=query,
        render_max_total_chars=render_max_total_chars,
        render_max_chars_per_item=render_max_chars_per_item,
    )
    return {
        "domain": domain,
        "question_id": str(row.get("question_id") or ""),
        "artifact_trace_mode": artifact_trace_mode(row),
        "phrase_set_eligible": bool(phrases),
        "baseline_full_phrase_exposure": baseline_exposed,
        "candidate_full_phrase_exposure": candidate_exposed,
        "context_changed": baseline_keys != candidate_keys,
        "raw_assembly_matches_baseline": (
            assembled_keys[: len(baseline_raw_keys)] == baseline_raw_keys
        ),
        "baseline_typed_count": len(typed),
        "typed_entity_type_fallback_count": sum(
            bool(item.get("_entity_type_fallback")) for item in typed
        ),
        "candidate_typed_count": composition["selected_typed_count"],
        "candidate_neighbor_count": sum(
            item.get("_selection_origin") == "neighbor" for item in candidate
        ),
        "candidate_state_part_count": sum(
            item.get("_selection_origin") == "state_part" for item in candidate
        ),
        "candidate_state_refinement_count": len(
            candidate_assembly["state_part_refinement"]["replacements"]
        ),
        "baseline_keys": baseline_keys,
        "candidate_keys": candidate_keys,
        "assembled_raw_keys": assembled_keys,
        "composition": composition,
        "render_screens": render_screens,
    }


def replay_question_render_levers(
    *,
    row: dict[str, Any],
    candidates: list[dict[str, object]],
    action_spines: dict[str, dict[str, object]],
    max_items: int,
    query: str,
    render_max_total_chars: int,
    render_max_chars_per_item: int,
) -> dict[str, dict[str, object]]:
    """Exercise every render lever independently on one frozen selected pack."""
    _control_context, control_budget = render_memory_context(
        candidates,
        query=query,
        max_items=max_items,
        max_chars_per_item=render_max_chars_per_item,
        max_total_chars=DEFAULT_CONTEXT_TOTAL_CHARS,
    )
    _treatment_context, treatment_budget = render_memory_context(
        candidates,
        query=query,
        max_items=max_items,
        max_chars_per_item=render_max_chars_per_item,
        max_total_chars=render_max_total_chars,
    )
    char_receipt = reader_char_total_activity(
        control_receipt=control_budget,
        treatment_receipt=treatment_budget,
    )
    char_activity = int(char_receipt["promoted_to_full_count"])
    char_within = _receipt_within_hard_total(treatment_budget, render_max_total_chars)

    grouped, lane_receipt = group_results_by_lane(candidates)
    _lane_context, lane_budget = render_memory_context(
        grouped,
        query=query,
        max_items=max_items,
        max_chars_per_item=render_max_chars_per_item,
        max_total_chars=render_max_total_chars,
    )
    lane_activity = int(lane_receipt["nonempty_lane_count"])
    lane_within = _receipt_within_hard_total(lane_budget, render_max_total_chars)

    with_spines, action_receipt = append_action_spines(
        candidates,
        sidecars=action_spines,
    )
    _action_context, action_budget = render_memory_context(
        with_spines,
        query=query,
        max_items=max_items + int(action_receipt["appended_spine_count"]),
        max_chars_per_item=render_max_chars_per_item,
        max_total_chars=render_max_total_chars,
    )
    rendered_ids = {
        str(item.get("entity_id") or "")
        for item in action_budget.get("items", [])
        if isinstance(item, dict) and item.get("dropped") is not True
    }
    action_activity = sum(entity_id.startswith("action-spine:") for entity_id in rendered_ids)
    action_within = _receipt_within_hard_total(action_budget, render_max_total_chars)
    action_missing = list(action_receipt["missing_trajectory_ids"])

    metadata = row.get("memory_post_query_metadata")
    search_metadata = metadata.get("search_metadata") if isinstance(metadata, dict) else None
    composition = (
        search_metadata.get("evidence_composition")
        if isinstance(search_metadata, dict)
        else None
    )
    production_screens = screen_context_composition_receipt(
        composition if isinstance(composition, dict) else None
    )
    return {
        LEVER_CONTEXT_TOTAL_CHARS: {
            "status": (
                "survived"
                if char_activity and char_within
                else "blocked_hard_total" if not char_within else "blocked_no_treatment_activity"
            ),
            "survives": bool(char_activity) and char_within,
            "activity_events": char_activity,
            "hard_total_within": char_within,
            "receipt": char_receipt,
        },
        **production_screens,
        LEVER_ENGLISH_LANE_GROUPING: {
            "status": (
                "survived"
                if lane_activity
                and lane_within
                and lane_receipt["membership_preserved"]
                and lane_receipt["within_lane_order_preserved"]
                else "blocked_render_invariant"
            ),
            "survives": bool(
                lane_activity
                and lane_within
                and lane_receipt["membership_preserved"]
                and lane_receipt["within_lane_order_preserved"]
            ),
            "activity_events": lane_activity if lane_within else 0,
            "hard_total_within": lane_within,
            "receipt": lane_receipt,
        },
        LEVER_ACTION_SPINES: {
            "status": (
                "survived"
                if action_activity and action_within and not action_missing
                else (
                    "blocked_missing_treatment_artifact"
                    if action_missing
                    else "blocked_hard_total" if not action_within else "blocked_no_treatment_activity"
                )
            ),
            "survives": bool(action_activity) and action_within and not action_missing,
            "activity_events": action_activity if action_within and not action_missing else 0,
            "hard_total_within": action_within,
            "receipt": action_receipt,
        },
    }


def _receipt_within_hard_total(receipt: dict[str, object], hard_total_chars: int) -> bool:
    rendered = receipt.get("rendered_context_chars")
    configured = receipt.get("max_total_chars")
    return (
        isinstance(rendered, int)
        and not isinstance(rendered, bool)
        and rendered <= hard_total_chars
        and configured == hard_total_chars
    )


def aggregate_render_screens(
    *,
    rows: list[dict[str, Any]],
    distillation_screens_by_domain: dict[str, dict[str, dict[str, object]]],
    hard_total_chars: int,
) -> dict[str, object]:
    """Combine score-blind screens without converting missing artifacts into evidence."""
    per_lever: dict[str, list[dict[str, object]]] = {
        lever: [] for lever in RENDER_BUNDLE_LEVERS
    }
    for row in rows:
        screens = row.get("render_screens")
        if not isinstance(screens, dict):
            continue
        for lever in RENDER_BUNDLE_LEVERS:
            if lever in {"observed_absence", "digest_roles_budget"}:
                continue
            screen = screens.get(lever)
            if isinstance(screen, dict):
                per_lever[lever].append(screen)
    for domain in sorted(distillation_screens_by_domain):
        screens = distillation_screens_by_domain[domain]
        for lever in ("observed_absence", "digest_roles_budget"):
            screen = screens.get(lever)
            if isinstance(screen, dict):
                per_lever[lever].append({**screen, "domain": domain})

    fatal_statuses = {
        "blocked_missing_treatment_artifact",
        "blocked_invalid_treatment_artifact",
        "blocked_raw_parity",
        "blocked_hard_budget",
        "blocked_hard_total",
        "blocked_render_invariant",
    }
    lever_summary: dict[str, dict[str, object]] = {}
    for lever in RENDER_BUNDLE_LEVERS:
        screens = per_lever[lever]
        activity = sum(
            int(screen.get("activity_events", 0))
            for screen in screens
            if isinstance(screen.get("activity_events", 0), int)
            and not isinstance(screen.get("activity_events", 0), bool)
        )
        fatal = next(
            (
                str(screen.get("status"))
                for screen in screens
                if screen.get("status") in fatal_statuses
            ),
            None,
        )
        survives = bool(screens) and fatal is None and activity > 0
        lever_summary[lever] = {
            "status": (
                "survived"
                if survives
                else fatal or "blocked_no_treatment_activity"
                if screens
                else "blocked_missing_treatment_artifact"
            ),
            "survives": survives,
            "activity_events": activity,
            "screen_count": len(screens),
        }

    observed_render_chars = [
        int(receipt.get("rendered_context_chars"))
        for row in rows
        for screen in (
            row.get("render_screens", {}).values()
            if isinstance(row.get("render_screens"), dict)
            else []
        )
        if isinstance(screen, dict)
        and isinstance((receipt := screen.get("receipt")), dict)
        and isinstance(receipt.get("rendered_context_chars"), int)
        and not isinstance(receipt.get("rendered_context_chars"), bool)
    ]
    max_observed = max(observed_render_chars, default=0)
    hard_total_within = max_observed <= hard_total_chars
    if not hard_total_within:
        for lever in RENDER_BUNDLE_LEVERS:
            lever_summary[lever]["status"] = "blocked_hard_total"
            lever_summary[lever]["survives"] = False
    return {
        "score_blind": True,
        "hard_total_chars": hard_total_chars,
        "max_observed_rendered_chars": max_observed,
        "hard_total_within": hard_total_within,
        "levers": lever_summary,
        "survivor_set": [
            lever for lever in RENDER_BUNDLE_LEVERS if lever_summary[lever]["survives"]
        ],
        "bundle_eligible": all(lever_summary[lever]["survives"] for lever in RENDER_BUNDLE_LEVERS),
    }


def result_candidates(row: dict[str, Any]) -> list[dict[str, object]]:
    contexts = row.get("memory_context")
    metadata = row.get("memory_post_query_metadata")
    if not isinstance(contexts, list) or not isinstance(metadata, dict):
        raise TypeError("Run row is missing memory context metadata")
    trace = metadata.get("retrieval_trace")
    if trace is None or trace == []:
        return legacy_result_candidates(contexts)
    if not isinstance(trace, list) or len(trace) != len(contexts):
        raise ValueError("Retrieval trace and memory context lengths disagree")
    candidates: list[dict[str, object]] = []
    for trace_item, context_item in zip(trace, contexts, strict=True):
        if not isinstance(trace_item, dict) or not isinstance(context_item, dict):
            raise TypeError("Invalid retrieval trace item")
        value = context_item.get("value")
        if not isinstance(value, str):
            raise TypeError("Memory context item is missing text")
        _header, separator, content = value.partition("\n\n")
        state_indices = trace_item.get("state_indices") or []
        candidates.append(
            {
                "id": str(trace_item.get("entity_id") or ""),
                "type": _trace_entity_type(trace_item),
                "content": content if separator else value,
                "score": trace_item.get("score"),
                "metadata": {
                    "longmemeval_v2_trajectory_id": trace_item.get("trajectory_id"),
                    "longmemeval_v2_chunk_index": trace_item.get("chunk_index"),
                    "longmemeval_v2_state_index": (
                        state_indices[0]
                        if isinstance(state_indices, list) and len(state_indices) == 1
                        else None
                    ),
                    "longmemeval_v2_state_indices": state_indices,
                },
                "_selection_origin": trace_item.get("selection_origin"),
                "_search_rank": trace_item.get("search_rank"),
                "_state_part_of_search_rank": trace_item.get("state_part_of_search_rank"),
                "_state_part_refined_from_chunk": trace_item.get("state_part_refined_from_chunk"),
                "_neighbor_of_search_rank": trace_item.get("neighbor_of_search_rank"),
                "_neighbor_distance": trace_item.get("neighbor_distance"),
                "_entity_type_fallback": (
                    str(trace_item.get("selection_origin") or "").startswith("context_pack:")
                    and not str(trace_item.get("entity_type") or "").strip()
                ),
            }
        )
    return candidates


def _trace_entity_type(trace_item: dict[str, object]) -> str:
    if entity_type := str(trace_item.get("entity_type") or "").strip():
        return entity_type
    if str(trace_item.get("selection_origin") or "").startswith("context_pack:"):
        return "procedure"
    return "session"


def artifact_trace_mode(row: dict[str, Any]) -> str:
    metadata = row.get("memory_post_query_metadata")
    trace = metadata.get("retrieval_trace") if isinstance(metadata, dict) else None
    return "retrieval_trace" if isinstance(trace, list) and trace else "legacy_context_headers"


def legacy_result_candidates(contexts: list[object]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for context_item in contexts:
        if not isinstance(context_item, dict):
            raise TypeError("Invalid legacy memory context item")
        value = context_item.get("value")
        if not isinstance(value, str):
            raise TypeError("Legacy memory context item is missing text")
        header, separator, content = value.partition("\n\n")
        match = _LEGACY_CONTEXT_HEADER.fullmatch(header)
        if match is None or not separator:
            raise ValueError("Legacy memory context header is not replayable")
        rank = int(match.group("rank"))
        chunk_index = int(match.group("chunk"))
        trajectory_id = match.group("trajectory").strip()
        candidates.append(
            {
                "id": f"legacy:{trajectory_id}:{chunk_index}",
                "type": "session",
                "content": content,
                "score": float(match.group("score")),
                "metadata": {
                    "longmemeval_v2_trajectory_id": trajectory_id,
                    "longmemeval_v2_chunk_index": chunk_index,
                    "longmemeval_v2_state_index": chunk_index,
                    "longmemeval_v2_state_indices": [chunk_index],
                },
                "_selection_origin": "search",
                "_search_rank": rank,
            }
        )
    return candidates


def load_chunk_catalog(path: Path) -> dict[str, dict[int, dict[str, object]]]:
    catalog: dict[str, dict[int, dict[str, object]]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            metadata = item.get("metadata")
            if not isinstance(metadata, dict):
                continue
            trajectory_id = metadata.get("longmemeval_v2_trajectory_id")
            chunk_index = metadata.get("longmemeval_v2_chunk_index")
            if isinstance(trajectory_id, str) and isinstance(chunk_index, int):
                catalog.setdefault(trajectory_id, {})[chunk_index] = item
    return catalog


def load_memory_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Memory manifest is not an object: {path}")
    return payload


def load_bound_render_artifacts(
    *,
    manifest: dict[str, object],
    manifest_path: Path,
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, object],
]:
    action_spines: dict[str, dict[str, object]] = {}
    distillation_receipts: dict[str, dict[str, object]] = {}
    source_digests: dict[str, object] = {}

    action_digest = manifest.get("action_spines_sha256")
    if action_digest is not None:
        action_path = manifest_path.parent / ACTION_SPINE_FILENAME
        _validate_bound_sidecar(
            path=action_path,
            expected_digest=action_digest,
            label="action-spine",
        )
        action_spines = read_action_spines(action_path, compressed=True)
        if manifest.get("action_spine_count") != len(action_spines):
            raise ValueError("Memory manifest action-spine count is invalid")
        source_digests["action_spines_sha256"] = file_sha256(action_path)

    distillation_digest = manifest.get("distillation_receipts_sha256")
    if distillation_digest is not None:
        distillation_path = manifest_path.parent / DISTILLATION_RECEIPT_FILENAME
        _validate_bound_sidecar(
            path=distillation_path,
            expected_digest=distillation_digest,
            label="distillation-receipt",
        )
        distillation_receipts = read_distillation_receipts(
            distillation_path,
            compressed=True,
        )
        if manifest.get("distillation_receipt_count") != len(distillation_receipts):
            raise ValueError("Memory manifest distillation-receipt count is invalid")
        receipt_set_digest = canonical_sha256(distillation_receipts)
        manifest_set_digest = manifest.get("ingest_note_distillation_receipt_set_sha256")
        if manifest_set_digest is not None and manifest_set_digest != receipt_set_digest:
            raise ValueError("Memory manifest distillation receipt-set digest is invalid")
        source_digests["distillation_receipts_sha256"] = file_sha256(distillation_path)
        source_digests["distillation_receipt_set_sha256"] = receipt_set_digest
    return action_spines, distillation_receipts, source_digests


def _validate_bound_sidecar(
    *,
    path: Path,
    expected_digest: object,
    label: str,
) -> None:
    if not path.is_file():
        raise ValueError(f"Memory manifest binds a missing {label} sidecar")
    if expected_digest != file_sha256(path):
        raise ValueError(f"Memory manifest {label} sidecar digest is invalid")


def validate_memory_binding(
    *,
    manifest: dict[str, object],
    catalog_path: Path,
    rows: list[dict[str, Any]],
) -> None:
    catalog_sha256 = sha256_file(catalog_path)
    if manifest.get("chunk_catalog_sha256") != catalog_sha256:
        raise ValueError("Memory manifest does not bind the supplied chunk catalog")
    expected_run_id = str(manifest.get("run_id") or "")
    expected_project_id = str(manifest.get("project_id") or "")
    if not expected_run_id or not expected_project_id:
        raise ValueError("Memory manifest is missing run or project identity")
    for row in rows:
        metadata = row.get("memory_post_query_metadata")
        if not isinstance(metadata, dict):
            raise TypeError("Run row is missing memory post-query metadata")
        if str(metadata.get("run_id") or "") != expected_run_id:
            raise ValueError("Run row does not match the memory manifest run identity")
        if str(metadata.get("project_id") or "") != expected_project_id:
            raise ValueError("Run row does not match the memory manifest project identity")


def validate_render_receipt_binding(
    *,
    rows: list[dict[str, Any]],
    distillation_receipts: dict[str, dict[str, object]],
) -> None:
    if not distillation_receipts:
        return
    expected_digest = canonical_sha256(distillation_receipts)
    for row in rows:
        metadata = row.get("memory_post_query_metadata")
        if not isinstance(metadata, dict):
            raise TypeError("Run row is missing memory post-query metadata")
        if metadata.get("ingest_note_distillation_receipt_set_sha256") != expected_digest:
            raise ValueError("Run row does not bind the production distillation receipt set")
        if metadata.get("ingest_note_distillation_receipt_count") != len(
            distillation_receipts
        ):
            raise ValueError("Run row distillation receipt count is invalid")


def validate_seed_catalog_content(
    candidates: list[dict[str, object]],
    *,
    chunk_catalog: dict[str, dict[int, dict[str, object]]],
) -> None:
    for candidate in candidates:
        if str(candidate.get("_selection_origin") or "") != "search":
            continue
        metadata = candidate.get("metadata")
        if not isinstance(metadata, dict):
            raise TypeError("Search seed is missing replay metadata")
        trajectory_id = str(metadata.get("longmemeval_v2_trajectory_id") or "")
        chunk_index = metadata.get("longmemeval_v2_chunk_index")
        catalog_item = (
            chunk_catalog.get(trajectory_id, {}).get(chunk_index)
            if isinstance(chunk_index, int)
            else None
        )
        if catalog_item is None:
            raise ValueError("Search seed is absent from the bound chunk catalog")
        sealed_content = " ".join(str(candidate.get("content") or "").split())
        catalog_content = " ".join(str(catalog_item.get("content") or "").split())
        if sealed_content != catalog_content:
            raise ValueError("Search seed content disagrees with the bound chunk catalog")


def answer_phrases(row: dict[str, Any]) -> list[str]:
    evaluator = str(row.get("eval_function") or "")
    if not evaluator.startswith(_PHRASE_SET_EVALUATOR):
        return []
    answer = str(row.get("answer_gold") or "")
    return [phrase for part in re.split(r"[;,]", answer) if (phrase := normalize_text(part))]


def full_phrase_exposure(
    phrases: list[str],
    candidates: list[dict[str, object]],
) -> bool:
    if not phrases:
        return False
    context = normalize_text("\n".join(str(item.get("content") or "") for item in candidates))
    return all(phrase in context for phrase in phrases)


def normalize_text(value: str) -> str:
    translation = str.maketrans({character: " " for character in string.punctuation})
    return " ".join(value.lower().replace("-", " ").translate(translation).split())


def candidate_key(candidate: dict[str, object]) -> str:
    origin = str(candidate.get("_selection_origin") or "")
    metadata = candidate.get("metadata")
    if origin.startswith("context_pack:") or not isinstance(metadata, dict):
        return str(candidate.get("id") or "")
    return ":".join(
        (
            str(metadata.get("longmemeval_v2_trajectory_id") or ""),
            str(
                metadata.get("longmemeval_v2_chunk_index")
                if metadata.get("longmemeval_v2_chunk_index") is not None
                else ""
            ),
        )
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())
