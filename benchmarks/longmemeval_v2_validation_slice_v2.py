#!/usr/bin/env python3
"""Freeze the second score-blind LongMemEval-V2 composition slice."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks import longmemeval_v2_validation_slice as v1  # noqa: E402
from benchmarks.longmemeval_v2_causal_ablation import (  # noqa: E402
    select_stratified_questions,
)
from benchmarks.longmemeval_v2_reader_report import DOMAINS, sha256_file  # noqa: E402

SCHEMA_VERSION = "sibyl-longmemeval-v2-validation-slice-v2"
QUESTIONS_PER_DOMAIN = 45
SAMPLE_SEED = 2_026_071_602
PREDECESSOR_MANIFEST_SHA256 = (
    "sha256:e0293a26c268a6ff2e9109b6005dff18e075728bfd1b9ac71467a8e38df36c55"
)
EXCLUSION_LINEAGE_SHA256 = {
    "composition_v1_inherited_exclusions": {
        "web": "f03e9ceb1aab9da39f8ea95e9174d5b112cbfbc0555ad593dbb2c458e14913b3",
        "enterprise": "1fcb40f92a7c67afd7cad171064fd131bcb32ce217c04ec2e4e5e90b7258428a",
    },
    "composition_v1_candidate": {
        "web": "c4c58fa3f5da606d19212b81df8c2d9e272695189a6680beb977d49f95f204a0",
        "enterprise": "e43eb975477e87ab4224838aa1e5cf0b3a9dfb901e95e8c70ea49646e83a13a6",
    },
}
BASELINE_SELECTED_SCORES_SHA256 = {
    "web": "sha256:4752ed1c8afe8f014dc096dbda8f9ddaf27429ce095206e544384405a07bf940",
    "enterprise": ("sha256:6e7962b2f429c9760bef009ec8822fced720b6e9271b72c477214fa056853630"),
}
INTEGRITY_CONTRACT = {
    **v1.INTEGRITY_CONTRACT,
    "predecessor_validation_slice_excluded": True,
}
CANDIDATE_CONFIGURATION = {
    **v1.CANDIDATE_CONFIGURATION,
    "max_context_total_chars": 60_000,
}
DECISION_RULE = v1.DECISION_RULE
SELECTED_QUESTION_IMAGES = {
    "1e77eb4b": {
        "path": "question_screenshots/1e77eb4b.png",
        "sha256": "sha256:196b973d7cac067e76cf19f8ddff1cd7602739d4940e974d9154704f5d63eb3d",
    },
    "42d13006": {
        "path": "question_screenshots/42d13006.png",
        "sha256": "sha256:e005d86ecc6b595e593b5a88fb89172812d1d5c2d4de932f806ce0b5e3b5d569",
    },
    "626e401e": {
        "path": "question_screenshots/626e401e.png",
        "sha256": "sha256:fda1f973a575430256888c2177a933eb049f65a4645746699996b1ec9d511d3f",
    },
    "8e21c6e5": {
        "path": "question_screenshots/8e21c6e5.png",
        "sha256": "sha256:abaec4759d3b7d463419012bdec1c91daed23268fb9cd0fa77435380b7c69a86",
    },
    "fa504f5e": {
        "path": "question_screenshots/fa504f5e.png",
        "sha256": "sha256:551d6faa51fd82d3ba1529a6cc3944742b9f3318dded3275ba3431eb7c1ab0d8",
    },
}


def build_validation_slice(
    *,
    questions_path: Path,
    predecessor_manifest_path: Path,
    baseline_run_dirs: dict[str, Path],
    created_at: str | None = None,
    require_pinned_predecessor: bool = True,
) -> dict[str, Any]:
    manifest = _build_validation_slice(
        questions_path=questions_path,
        predecessor_manifest_path=predecessor_manifest_path,
        baseline_run_dirs=baseline_run_dirs,
        created_at=created_at,
        require_pinned_predecessor=require_pinned_predecessor,
    )
    require_validation_slice(
        manifest,
        require_pinned_predecessor=require_pinned_predecessor,
    )
    return manifest


def _build_validation_slice(
    *,
    questions_path: Path,
    predecessor_manifest_path: Path,
    baseline_run_dirs: dict[str, Path],
    created_at: str | None,
    require_pinned_predecessor: bool,
) -> dict[str, Any]:
    v1.require_domain_paths(baseline_run_dirs)
    predecessor = load_predecessor_manifest(
        predecessor_manifest_path,
        require_pinned=require_pinned_predecessor,
    )
    predecessor_selection = predecessor["selection"]
    exclusion_lineage = {
        "composition_v1_inherited_exclusions": {
            domain: sorted(predecessor_selection["excluded_question_ids_by_domain"][domain])
            for domain in DOMAINS
        },
        "composition_v1_candidate": {
            domain: sorted(predecessor_selection["question_ids_by_domain"][domain])
            for domain in DOMAINS
        },
    }
    excluded = v1.exclusion_union(exclusion_lineage)
    pool = v1.load_question_metadata(questions_path, excluded=excluded)
    selection = select_stratified_questions(
        pool,
        questions_per_domain=QUESTIONS_PER_DOMAIN,
        seed=SAMPLE_SEED,
        source="unseen_question_pool_after_composition_v1",
        excluded={domain: sorted(excluded[domain]) for domain in DOMAINS},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at or datetime.now(UTC).isoformat(),
        "protocol": {
            "question_count": QUESTIONS_PER_DOMAIN * len(DOMAINS),
            "questions_per_domain": QUESTIONS_PER_DOMAIN,
            "candidate_passes": 1,
            "fixed_sample_no_sequential_stopping": True,
            "score_visibility_during_selection": "none",
            "claim_level": "representative score-blind successor candidate screen",
        },
        "integrity_contract": INTEGRITY_CONTRACT,
        "candidate_configuration": CANDIDATE_CONFIGURATION,
        "decision_rule": DECISION_RULE,
        "exclusion_lineage": exclusion_lineage,
        "selection": selection,
        "frozen_baseline": v1.build_frozen_baseline(
            baseline_run_dirs,
            selected_ids=selection["question_ids_by_domain"],
        ),
        "selected_question_ids_sha256_by_domain": {
            domain: v1.sha256_question_ids(selection["question_ids_by_domain"][domain])
            for domain in DOMAINS
        },
        "excluded_question_ids_sha256_by_domain": {
            domain: v1.sha256_question_ids(selection["excluded_question_ids_by_domain"][domain])
            for domain in DOMAINS
        },
        "source_artifacts": {
            "dataset": {
                name: v1.source_artifact(path)
                for name, path in v1.validation_dataset_paths(questions_path).items()
            },
            "selected_question_images": v1.selected_question_image_sources(
                questions_path,
                selected_ids=selection["question_ids_by_domain"],
            ),
            "predecessor_manifest": v1.source_artifact(predecessor_manifest_path),
            "baseline_run_dirs": {
                domain: {
                    "path": str(baseline_run_dirs[domain]),
                    "files": {
                        name: sha256_file(baseline_run_dirs[domain] / name)
                        for name in v1.BASELINE_RUN_FILES
                    },
                }
                for domain in DOMAINS
            },
        },
    }


def require_validation_slice(
    manifest: dict[str, Any],
    *,
    require_pinned_predecessor: bool = True,
) -> None:
    require_manifest_contract(manifest)
    sources = manifest.get("source_artifacts")
    if not isinstance(sources, dict):
        raise TypeError("Validation slice has no source artifacts")
    dataset_sources = sources.get("dataset")
    baseline_sources = sources.get("baseline_run_dirs")
    if not isinstance(dataset_sources, dict):
        raise TypeError("Validation slice has no dataset sources")
    if not isinstance(baseline_sources, dict):
        raise TypeError("Validation slice has no baseline sources")
    dataset_paths = {
        name: v1.require_source(dataset_sources.get(name), name=f"dataset.{name}")
        for name in v1.VALIDATION_DATASET_FILES
    }
    predecessor_path = v1.require_source(
        sources.get("predecessor_manifest"),
        name="predecessor_manifest",
    )
    baseline_paths = {
        domain: v1.require_run_dir_source(
            baseline_sources.get(domain),
            name=f"baseline_run_dirs.{domain}",
        )
        for domain in DOMAINS
    }
    rebuilt = _build_validation_slice(
        questions_path=dataset_paths["questions_sha256"],
        predecessor_manifest_path=predecessor_path,
        baseline_run_dirs=baseline_paths,
        created_at=str(manifest.get("created_at") or ""),
        require_pinned_predecessor=require_pinned_predecessor,
    )
    if manifest != rebuilt:
        raise ValueError("Validation slice changed from its score-blind source selection")


def validate_frozen_manifest(
    manifest: dict[str, Any],
    *,
    questions_path: Path,
    require_pinned_sources: bool = True,
) -> None:
    require_manifest_contract(manifest)
    lineage = manifest.get("exclusion_lineage")
    selection = manifest.get("selection")
    sources = manifest.get("source_artifacts")
    if not isinstance(lineage, dict):
        raise TypeError("Validation slice has no exclusion lineage")
    if not isinstance(selection, dict):
        raise TypeError("Validation slice has no selection")
    if not isinstance(sources, dict):
        raise TypeError("Validation slice has no source artifacts")
    if require_pinned_sources:
        validate_exclusion_lineage(lineage)
    validate_frozen_sources(
        sources,
        questions_path=questions_path,
        selected_ids=selection["question_ids_by_domain"],
        require_pinned_sources=require_pinned_sources,
    )
    excluded = v1.exclusion_union(lineage)
    expected = select_stratified_questions(
        v1.load_question_metadata(questions_path, excluded=excluded),
        questions_per_domain=QUESTIONS_PER_DOMAIN,
        seed=SAMPLE_SEED,
        source="unseen_question_pool_after_composition_v1",
        excluded={domain: sorted(excluded[domain]) for domain in DOMAINS},
    )
    if selection != expected:
        raise ValueError("Validation slice selection changed")
    for domain in DOMAINS:
        selected = selection["question_ids_by_domain"][domain]
        excluded_ids = selection["excluded_question_ids_by_domain"][domain]
        if manifest.get("selected_question_ids_sha256_by_domain", {}).get(domain) != (
            v1.sha256_question_ids(selected)
        ):
            raise ValueError(f"Validation slice {domain} selected hash changed")
        if manifest.get("excluded_question_ids_sha256_by_domain", {}).get(domain) != (
            v1.sha256_question_ids(excluded_ids)
        ):
            raise ValueError(f"Validation slice {domain} excluded hash changed")
    validate_frozen_baseline(
        manifest.get("frozen_baseline"),
        selection=selection,
        require_pinned_sources=require_pinned_sources,
    )


def require_manifest_contract(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Invalid validation slice schema")
    if manifest.get("integrity_contract") != INTEGRITY_CONTRACT:
        raise ValueError("Validation slice integrity contract changed")
    if manifest.get("candidate_configuration") != CANDIDATE_CONFIGURATION:
        raise ValueError("Validation slice candidate configuration changed")
    if manifest.get("decision_rule") != DECISION_RULE:
        raise ValueError("Validation slice decision rule changed")


def validate_exclusion_lineage(lineage: dict[str, Any]) -> None:
    if set(lineage) != set(EXCLUSION_LINEAGE_SHA256):
        raise ValueError("Validation slice exclusion lineage sources changed")
    for source, expected_by_domain in EXCLUSION_LINEAGE_SHA256.items():
        ids_by_domain = lineage.get(source)
        if not isinstance(ids_by_domain, dict) or set(ids_by_domain) != set(DOMAINS):
            raise ValueError(f"Validation slice exclusion lineage {source} changed")
        for domain, expected_sha256 in expected_by_domain.items():
            question_ids = ids_by_domain.get(domain)
            if (
                not isinstance(question_ids, list)
                or v1.sha256_question_ids(question_ids) != expected_sha256
            ):
                raise ValueError(f"Validation slice exclusion lineage {source}.{domain} changed")


def validate_frozen_sources(
    sources: dict[str, Any],
    *,
    questions_path: Path,
    selected_ids: dict[str, list[str]],
    require_pinned_sources: bool,
) -> None:
    v1.validate_frozen_dataset_sources(
        sources.get("dataset"),
        questions_path=questions_path,
        require_pinned_sources=require_pinned_sources,
    )
    predecessor = sources.get("predecessor_manifest")
    if not isinstance(predecessor, dict):
        raise TypeError("Validation slice predecessor source changed")
    predecessor_path = Path(str(predecessor.get("path") or ""))
    actual_predecessor_sha256 = sha256_file(predecessor_path)
    if predecessor.get("sha256") != actual_predecessor_sha256 or (
        require_pinned_sources and actual_predecessor_sha256 != PREDECESSOR_MANIFEST_SHA256
    ):
        raise ValueError("Validation slice predecessor source changed")
    expected_images = v1.selected_question_image_sources(
        questions_path,
        selected_ids=selected_ids,
    )
    if sources.get("selected_question_images") != expected_images:
        raise ValueError("Validation slice selected question images changed")
    if require_pinned_sources:
        pinned_images = {
            question_id: {
                "relative_path": source["path"],
                "sha256": source["sha256"],
            }
            for question_id, source in SELECTED_QUESTION_IMAGES.items()
        }
        if expected_images != pinned_images:
            raise ValueError("Validation slice selected question image pins changed")


def load_predecessor_manifest(path: Path, *, require_pinned: bool) -> dict[str, Any]:
    if require_pinned and sha256_file(path) != PREDECESSOR_MANIFEST_SHA256:
        raise ValueError("Validation slice predecessor manifest changed")
    predecessor = v1.load_json(path)
    if not isinstance(predecessor, dict) or predecessor.get("schema_version") != v1.SCHEMA_VERSION:
        raise ValueError("Validation slice predecessor manifest is invalid")
    selection = predecessor.get("selection")
    if not isinstance(selection, dict):
        raise TypeError("Validation slice predecessor has no selection")
    for key in ("excluded_question_ids_by_domain", "question_ids_by_domain"):
        ids_by_domain = selection.get(key)
        if not isinstance(ids_by_domain, dict) or set(ids_by_domain) != set(DOMAINS):
            raise TypeError(f"Validation slice predecessor has invalid {key}")
    return predecessor


def validate_candidate_configuration(
    manifest: dict[str, Any],
    *,
    tier: str,
    reader_base_url: str,
    reader_model: str,
    reader_max_concurrent_requests: int,
    reader_retry_attempts: int,
    evaluator_model: str,
    evidence_composition_mode: str,
    source_evidence_bundling: bool,
    include_screenshot_refs: bool,
    max_context_total_chars: int,
) -> None:
    actual = {
        "tier": tier,
        "official_harness_commit": v1.OFFICIAL_HARNESS_COMMIT,
        "reader_base_url": reader_base_url,
        "reader_model": reader_model,
        "reader_max_concurrent_requests": reader_max_concurrent_requests,
        "reader_retry_attempts": reader_retry_attempts,
        "evaluator_model": evaluator_model,
        "evidence_composition_mode": evidence_composition_mode,
        "source_evidence_bundling": source_evidence_bundling,
        "include_screenshot_refs": include_screenshot_refs,
        "max_context_total_chars": max_context_total_chars,
    }
    if actual != manifest.get("candidate_configuration"):
        raise ValueError(f"Candidate configuration does not match frozen manifest: {actual}")


def validate_frozen_baseline(
    raw: Any,
    *,
    selection: dict[str, Any],
    require_pinned_sources: bool = True,
) -> None:
    v1.validate_frozen_baseline(
        raw,
        selection=selection,
        require_pinned_sources=False,
    )
    if not require_pinned_sources:
        return
    if not isinstance(raw, dict) or not isinstance(raw.get("domains"), dict):
        raise TypeError("Validation slice has no frozen baseline domains")
    domains = raw["domains"]
    for domain in DOMAINS:
        baseline = domains[domain]
        if (
            v1.sha256_json(baseline["selected_scores_by_question_id"])
            != BASELINE_SELECTED_SCORES_SHA256[domain]
        ):
            raise ValueError(f"Validation slice frozen baseline {domain} score source changed")
        if baseline["source_sha256"] != v1.BASELINE_SOURCE_SHA256[domain]:
            raise ValueError(f"Validation slice frozen baseline {domain} source hashes changed")
        expected_full = v1.BASELINE_FULL_RESULTS[domain]
        if (
            baseline["full_question_count"] != expected_full["question_count"]
            or baseline["full_accuracy"] != expected_full["accuracy"]
        ):
            raise ValueError(f"Validation slice frozen baseline {domain} full result changed")


def evaluate_candidate(
    manifest: dict[str, Any],
    *,
    run_dirs: dict[str, Path],
    questions_path: Path,
    require_pinned_sources: bool = True,
) -> dict[str, Any]:
    validate_frozen_manifest(
        manifest,
        questions_path=questions_path,
        require_pinned_sources=require_pinned_sources,
    )
    validate_frozen_baseline(
        manifest.get("frozen_baseline"),
        selection=manifest["selection"],
        require_pinned_sources=require_pinned_sources,
    )
    for domain in DOMAINS:
        run_dir = run_dirs[domain]
        plan = v1.load_json(run_dir / "longmemeval_v2_official_plan.json")
        if (
            plan.get("max_context_total_chars")
            != CANDIDATE_CONFIGURATION["max_context_total_chars"]
        ):
            raise ValueError(f"Candidate {domain} context budget changed")
        validate_candidate_artifact_bindings(run_dir, domain=domain)
    return v1.evaluate_candidate(
        manifest,
        run_dirs=run_dirs,
        require_pinned_baseline=False,
    )


def validate_candidate_artifact_bindings(run_dir: Path, *, domain: str) -> None:
    plan_path = run_dir / "longmemeval_v2_official_plan.json"
    run_args_path = run_dir / "run_args.json"
    metrics_path = run_dir / "aggregated_metrics.json"
    scores_path = run_dir / "per_question.jsonl"
    runtime_dir = run_dir / "runtime_inputs"
    questions_path = runtime_dir / "questions.json"
    haystack_path = runtime_dir / "haystack.json"
    memory_config_path = runtime_dir / "memory_config.json"
    receipt = v1.load_json(run_dir / "longmemeval_v2_official_receipt.json")
    source_runs = receipt.get("source_runs") if isinstance(receipt, dict) else None
    if (
        not isinstance(source_runs, dict)
        or source_runs.get("expected_domains") != [domain]
        or source_runs.get("complete") is not True
        or source_runs.get("integrity_complete") is not True
        or source_runs.get("api_runtime_consistent") is not True
        or source_runs.get("model_consistent") is not True
        or source_runs.get("method_consistent") is not True
    ):
        raise ValueError(f"Candidate {domain} source-run receipt changed")
    domains = source_runs.get("domains")
    if not isinstance(domains, dict) or set(domains) != {domain}:
        raise ValueError(f"Candidate {domain} source-run domains changed")
    source_run = domains[domain]
    if not isinstance(source_run, dict):
        raise TypeError(f"Candidate {domain} source-run receipt is invalid")
    runtime_inputs = source_run.get("runtime_inputs")
    if not isinstance(runtime_inputs, dict) or set(runtime_inputs) != {
        "questions",
        "haystack",
        "memory_config",
    }:
        raise TypeError(f"Candidate {domain} runtime-input receipt is invalid")
    require_artifact_digest(source_run.get("plan"), plan_path, name=f"{domain} plan")
    require_artifact_digest(source_run.get("run_args"), run_args_path, name=f"{domain} run args")
    require_artifact_digest(
        source_run.get("aggregated_metrics"),
        metrics_path,
        name=f"{domain} aggregate metrics",
    )
    require_artifact_digest(
        source_run.get("per_question"),
        scores_path,
        name=f"{domain} per-question results",
    )
    require_artifact_digest(
        runtime_inputs.get("questions"),
        questions_path,
        name=f"{domain} runtime questions",
    )
    require_artifact_digest(
        runtime_inputs.get("haystack"),
        haystack_path,
        name=f"{domain} runtime haystack",
    )
    require_artifact_digest(
        runtime_inputs.get("memory_config"),
        memory_config_path,
        name=f"{domain} memory config",
    )
    validate_source_run_identity(source_run, run_args_path=run_args_path, domain=domain)
    validate_provider_usage_bindings(
        source_run.get("provider_usage"),
        run_dir=run_dir,
        plan=v1.load_json(plan_path),
        domain=domain,
    )
    memory_config = v1.load_json(memory_config_path)
    validate_candidate_memory_config(
        memory_config,
        name=f"Candidate {domain} runtime memory config",
    )
    validate_candidate_memory_config(
        source_run.get("effective_memory_config"),
        name=f"Candidate {domain} effective memory config",
    )


def require_artifact_digest(raw: Any, path: Path, *, name: str) -> None:
    if (
        not isinstance(raw, dict)
        or raw.get("exists") is not True
        or raw.get("sha256") != sha256_file(path)
    ):
        raise ValueError(f"Candidate {name} artifact binding changed")


def validate_source_run_identity(
    source_run: dict[str, Any],
    *,
    run_args_path: Path,
    domain: str,
) -> None:
    expected = {
        "reader_model": CANDIDATE_CONFIGURATION["reader_model"],
        "reader_base_url": CANDIDATE_CONFIGURATION["reader_base_url"],
        "evaluator_model": CANDIDATE_CONFIGURATION["evaluator_model"],
    }
    actual = {
        "reader_model": source_run.get("reader_model"),
        "reader_base_url": str(source_run.get("reader_base_url") or "").rstrip("/"),
        "evaluator_model": source_run.get("evaluator_model"),
    }
    if actual != expected or source_run.get("api_runtime_consistent") is not True:
        raise ValueError(f"Candidate {domain} source-run identity changed")

    run_args = v1.load_json(run_args_path)
    if {
        "domain": run_args.get("domain"),
        "model": run_args.get("model"),
        "base_url": str(run_args.get("base_url") or "").rstrip("/"),
        "evaluator_model": run_args.get("evaluator_model"),
        "reader_max_concurrent_requests": run_args.get("reader_max_concurrent_requests"),
    } != {
        "domain": domain,
        "model": CANDIDATE_CONFIGURATION["reader_model"],
        "base_url": CANDIDATE_CONFIGURATION["reader_base_url"],
        "evaluator_model": CANDIDATE_CONFIGURATION["evaluator_model"],
        "reader_max_concurrent_requests": CANDIDATE_CONFIGURATION["reader_max_concurrent_requests"],
    }:
        raise ValueError(f"Candidate {domain} run arguments changed")


def validate_provider_usage_bindings(
    raw: Any,
    *,
    run_dir: Path,
    plan: dict[str, Any],
    domain: str,
) -> None:
    if not isinstance(raw, dict) or set(raw) != {"reader", "judge"}:
        raise TypeError(f"Candidate {domain} provider-usage receipt is invalid")
    expected_run_id = plan.get("provider_usage_run_id") or plan.get("run_id")
    if not isinstance(expected_run_id, str) or not expected_run_id:
        raise ValueError(f"Candidate {domain} provider usage run ID changed")
    for role in ("reader", "judge"):
        path = run_dir / "provider_usage" / f"{role}.jsonl"
        record = raw.get(role)
        require_artifact_digest(record, path, name=f"{domain} {role} provider usage")
        if not isinstance(record, dict) or {
            "invalid_line_count": record.get("invalid_line_count"),
            "foreign_event_count": record.get("foreign_event_count"),
            "run_ids": record.get("run_ids"),
            "expected_run_id": record.get("expected_run_id"),
            "attempt_count": record.get("attempt_count"),
        } != {
            "invalid_line_count": 0,
            "foreign_event_count": 0,
            "run_ids": [expected_run_id],
            "expected_run_id": expected_run_id,
            "attempt_count": 1,
        }:
            raise ValueError(f"Candidate {domain} {role} provider-usage receipt changed")
        events = v1.load_jsonl(path)
        if (
            record.get("event_count") != len(events)
            or not events
            or any(
                not isinstance(event, dict)
                or event.get("role") != role
                or event.get("run_id") != expected_run_id
                or not isinstance(event.get("usage"), dict)
                for event in events
            )
        ):
            raise ValueError(f"Candidate {domain} {role} provider usage changed")


def validate_candidate_memory_config(raw: Any, *, name: str) -> None:
    params = raw.get("memory_params") if isinstance(raw, dict) else None
    expected = {
        key: CANDIDATE_CONFIGURATION[key]
        for key in (
            "max_context_total_chars",
            "evidence_composition_mode",
            "source_evidence_bundling",
            "include_screenshot_refs",
        )
    }
    if not isinstance(params, dict) or any(
        params.get(key) != value for key, value in expected.items()
    ):
        raise ValueError(f"{name} changed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--questions", required=True)
    generate.add_argument("--predecessor-manifest", required=True)
    generate.add_argument("--baseline-run", action="append", required=True, metavar="DOMAIN=DIR")
    generate.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--questions", required=True)
    verify.add_argument("--tier", required=True)
    verify.add_argument("--reader-base-url", required=True)
    verify.add_argument("--reader-model", required=True)
    verify.add_argument("--reader-max-concurrent-requests", required=True, type=int)
    verify.add_argument("--reader-retry-attempts", required=True, type=int)
    verify.add_argument("--evaluator-model", required=True)
    verify.add_argument("--evidence-composition-mode", required=True)
    verify.add_argument("--source-evidence-bundling", action="store_true")
    verify.add_argument("--include-screenshot-refs", action="store_true")
    verify.add_argument("--max-context-total-chars", required=True, type=int)
    verify.add_argument("--output", required=True)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--manifest", required=True)
    evaluate.add_argument("--questions", required=True)
    evaluate.add_argument("--run", action="append", required=True, metavar="DOMAIN=DIR")
    evaluate.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output)
    exit_code = 0
    if args.command == "generate":
        payload = build_validation_slice(
            questions_path=Path(args.questions),
            predecessor_manifest_path=Path(args.predecessor_manifest),
            baseline_run_dirs=v1.parse_domain_paths(args.baseline_run, option="--baseline-run"),
        )
    elif args.command == "verify":
        manifest = v1.load_json(Path(args.manifest))
        validate_frozen_manifest(manifest, questions_path=Path(args.questions))
        validate_candidate_configuration(
            manifest,
            tier=args.tier,
            reader_base_url=args.reader_base_url,
            reader_model=args.reader_model,
            reader_max_concurrent_requests=args.reader_max_concurrent_requests,
            reader_retry_attempts=args.reader_retry_attempts,
            evaluator_model=args.evaluator_model,
            evidence_composition_mode=args.evidence_composition_mode,
            source_evidence_bundling=args.source_evidence_bundling,
            include_screenshot_refs=args.include_screenshot_refs,
            max_context_total_chars=args.max_context_total_chars,
        )
        payload = {
            "status": "PASS",
            "question_ids_by_domain": manifest["selection"]["question_ids_by_domain"],
            "selected_question_ids_sha256_by_domain": manifest[
                "selected_question_ids_sha256_by_domain"
            ],
        }
    elif args.command == "evaluate":
        payload = evaluate_candidate(
            v1.load_json(Path(args.manifest)),
            run_dirs=v1.parse_domain_paths(args.run, option="--run"),
            questions_path=Path(args.questions),
        )
        exit_code = 0 if payload["decision"]["full_run_allowed"] else 1
    else:
        raise RuntimeError(f"Unknown command: {args.command}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))  # noqa: T201
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
