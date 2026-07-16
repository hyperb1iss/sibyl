#!/usr/bin/env python3
"""Freeze a score-blind LongMemEval-V2 validation slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.longmemeval_v2_causal_ablation import (  # noqa: E402
    select_stratified_questions,
)
from benchmarks.longmemeval_v2_reader_report import DOMAINS, sha256_file  # noqa: E402

SCHEMA_VERSION = "sibyl-longmemeval-v2-validation-slice-v1"
SHA256_HEX_LENGTH = 64
GIT_COMMIT_HEX_LENGTH = 40
QUESTIONS_PER_DOMAIN = 48
SAMPLE_SEED = 20_260_716
BASELINE_RUN_ID = 29_388_505_955
BASELINE_COMMIT = "9c2d5bcb3e680b21b535725dd6001d4e1c2bbf86"
OFFICIAL_HARNESS_COMMIT = "be15ea6e995462f3391c1a610892df3f67dfa7bd"
READER_BASE_URL = "https://openrouter.ai/api/v1"
BASELINE_ARTIFACTS = {
    "web": {
        "id": 8_335_287_194,
        "name": f"longmemeval-v2-web-small-{BASELINE_COMMIT}",
    },
    "enterprise": {
        "id": 8_333_726_277,
        "name": f"longmemeval-v2-enterprise-small-{BASELINE_COMMIT}",
    },
}
BASELINE_FULL_RESULTS = {
    "web": {"question_count": 240, "accuracy": 0.2875},
    "enterprise": {"question_count": 211, "accuracy": 0.3033175355450237},
}
BASELINE_SELECTED_SCORES_SHA256 = {
    "web": "sha256:c769de859611ba9ee596e40730573f9f11372d23b5458599d227d79d0b211fb4",
    "enterprise": "sha256:f7c127c24c37d3c64e3bce19d57f674da839649196b2ee30702116503e736b3e",
}
BASELINE_SOURCE_SHA256 = {
    "web": {
        "aggregated_metrics.json": (
            "sha256:215576f815267798ba30aa62c7bc4278db7e1a4a3b1be125a29741281fb6aa1c"
        ),
        "longmemeval_v2_official_plan.json": (
            "sha256:99060537a1b412de24370debd027fa98e53b5b955612b0b93eb604d8d65eb596"
        ),
        "longmemeval_v2_official_receipt.json": (
            "sha256:f191ebb78f87088794f877af0e251ac9a34955ff4c866c889b81c4562d921230"
        ),
        "per_question.jsonl": (
            "sha256:d5325b45e766a7093b441db03f54821e19c8e7ea33f215758d286f54edf21ec8"
        ),
    },
    "enterprise": {
        "aggregated_metrics.json": (
            "sha256:d2e28cfca3d984bade7931d083e25b08ea1bb2f312e5d92765b6e29140959cb4"
        ),
        "longmemeval_v2_official_plan.json": (
            "sha256:65c5e9d2f67063987f1b4ef431341338dd9c210b002c88a11fd9c2548a7a134d"
        ),
        "longmemeval_v2_official_receipt.json": (
            "sha256:44dbc04bc115721bdcd0ba2106c0f87f78903002e95ead5bc1feb3ca8c9ca73b"
        ),
        "per_question.jsonl": (
            "sha256:7cd32266fc120ed761cc2c8d6f10f3359ba788819f9419bd9580b1d9630c3c1a"
        ),
    },
}
BASELINE_RUN_FILES = (
    "aggregated_metrics.json",
    "longmemeval_v2_official_plan.json",
    "longmemeval_v2_official_receipt.json",
    "per_question.jsonl",
)
VALIDATION_DATASET_FILES = {
    "questions_sha256": Path("questions.jsonl"),
    "trajectories_sha256": Path("trajectories.jsonl"),
    "haystack_sha256": Path("haystacks/lme_v2_small.json"),
}
VALIDATION_DATASET_SHA256 = {
    "questions_sha256": "sha256:0a3ae5ebea938c24d7800e1e0b0828e08ae1646f939a53853b2b8cdc08e292b7",
    "trajectories_sha256": "sha256:363cec9a8e87aa8d9101ce4e600aadbf7031d674056ebe4f969e8424abc5f3c6",
    "haystack_sha256": "sha256:9b5301defb23a088a5f06e45ff8d5f35e569d78305a66d492046a9fff9b46593",
}
SELECTED_QUESTION_IMAGES = {
    "4964afed": {
        "path": "question_screenshots/4964afed.png",
        "sha256": "sha256:4c34dd2abc22e9afa87d6bbcf4340ffbce7eea4ec710713056c145b08021ca39",
    },
    "50d92f55": {
        "path": "question_screenshots/50d92f55.png",
        "sha256": "sha256:d66cad5875db8b052206fa0c75808d97a7c1aa25bc67e04b84481fa180a3de81",
    },
    "7586cf7c": {
        "path": "question_screenshots/7586cf7c.png",
        "sha256": "sha256:aff0665a68d29a2414d4a12ab21e1905de9a55087f2384f08fc9cf5bae587d47",
    },
    "81180ae9": {
        "path": "question_screenshots/81180ae9.png",
        "sha256": "sha256:eb088d8d7afebb9208a0c0cf9868f183a3d9b16b0c6674a9164b31315edd8a70",
    },
    "914ab1d4": {
        "path": "question_screenshots/914ab1d4.png",
        "sha256": "sha256:210883019e0ccb5f3c6cb7de6ef82588c097a05a2f1580afc1f6c21a5b530e75",
    },
    "b339209c": {
        "path": "question_screenshots/b339209c.png",
        "sha256": "sha256:9767f63510c5175cabcf89a6af498284e318fb990c5c8c24ec7d97572420998e",
    },
}
EXCLUSION_LINEAGE_SHA256 = {
    "prior_development_and_diagnostics": {
        "web": "08a65e81a209f1abf96997934a6276d358141f4869fe42f948fab21fc2a0892e",
        "enterprise": "49bc176e7ecd915f77e335cd01f25b8892b03dfc6b17ea321a60aba2773b5f18",
    },
    "unused_causal_confirmation": {
        "web": "cd2d6a7f0a81239636540eea2cbcfd04c913f34d2c23a9775fd3456777fac169",
        "enterprise": "4e1f12c41516f1c5687eef34c0efc7e1438cef8187f272a195d9c07782ba445d",
    },
    "prior_composition_candidate": {
        "web": "90a05168eaff3a4f8fc348ac50f37898f889a756fba6df1bad78b5df2ac38d8c",
        "enterprise": "819c273077aa6477adde610b44f7adaea4a4c840346605219ede857d528e87eb",
    },
}
EXCLUSION_SOURCE_SHA256 = {
    "causal_plan": "sha256:156efee9d971a3b6bb10b4f55d7a7e21c725da7d774e3387163f28ecea082830",
    "prior_run_questions": {
        "web": "sha256:93c6cb56cd0cff56243381f43dcccc01b1146fc2173049a77a783d4a51971dd6",
        "enterprise": ("sha256:92e46c1b38fa5855b49ee42520d57084d33b1ca237cadf3418c1cc050c46b4c4"),
    },
}
INTEGRITY_CONTRACT = {
    "sampling_inputs": ["question_id", "domain", "question_type"],
    "sampling_uses_question_text": False,
    "sampling_uses_answers_or_scores": False,
    "prior_candidate_questions_excluded": True,
    "prior_development_and_holdout_questions_excluded": True,
    "selection_frozen_before_candidate_scores": True,
    "interim_score_inspection_allowed": False,
}
CANDIDATE_CONFIGURATION = {
    "tier": "small",
    "official_harness_commit": OFFICIAL_HARNESS_COMMIT,
    "reader_base_url": READER_BASE_URL,
    "reader_model": "qwen/qwen3.5-9b",
    "reader_max_concurrent_requests": 16,
    "reader_retry_attempts": 4,
    "evaluator_model": "gpt-5.2",
    "evidence_composition_mode": "shared_relevance",
    "source_evidence_bundling": True,
    "include_screenshot_refs": False,
}
DECISION_RULE = {
    "purpose": "screen before another full-domain run",
    "minimum_accuracy_delta_over_frozen_baseline": 0.03,
    "minimum_domain_delta_over_frozen_baseline": -0.02,
    "pass_action": "run full official evaluation",
    "fail_action": "continue retrieval research without full evaluation",
    "promotion_decision": False,
}
RECEIPT_CHECK_STATUSES = {
    "official harness": "PASS",
    "dataset hashes": "PASS",
    "model pins": "PASS",
    "source runs": "PASS",
    "runtime provenance": "PASS",
    "leaderboard metrics": "FAIL",
    "accounting": "PASS",
    "approval boundary": "PASS",
}


def build_validation_slice(
    *,
    questions_path: Path,
    causal_plan_path: Path,
    prior_run_questions: dict[str, Path],
    baseline_run_dirs: dict[str, Path],
    created_at: str | None = None,
) -> dict[str, Any]:
    manifest = _build_validation_slice(
        questions_path=questions_path,
        causal_plan_path=causal_plan_path,
        prior_run_questions=prior_run_questions,
        baseline_run_dirs=baseline_run_dirs,
        created_at=created_at,
    )
    require_validation_slice(manifest)
    return manifest


def _build_validation_slice(
    *,
    questions_path: Path,
    causal_plan_path: Path,
    prior_run_questions: dict[str, Path],
    baseline_run_dirs: dict[str, Path],
    created_at: str | None,
) -> dict[str, Any]:
    require_domain_paths(prior_run_questions)
    require_domain_paths(baseline_run_dirs)
    causal_plan = load_json(causal_plan_path)
    confirmation = require_confirmation_selection(causal_plan)
    prior_ids = {
        domain: load_prior_question_ids(prior_run_questions[domain], domain=domain)
        for domain in DOMAINS
    }
    exclusion_lineage = {
        "prior_development_and_diagnostics": {
            domain: sorted(confirmation["excluded_question_ids_by_domain"][domain])
            for domain in DOMAINS
        },
        "unused_causal_confirmation": {
            domain: sorted(confirmation["question_ids_by_domain"][domain]) for domain in DOMAINS
        },
        "prior_composition_candidate": {domain: sorted(prior_ids[domain]) for domain in DOMAINS},
    }
    excluded = exclusion_union(exclusion_lineage)

    pool = load_question_metadata(questions_path, excluded=excluded)

    selection = select_stratified_questions(
        pool,
        questions_per_domain=QUESTIONS_PER_DOMAIN,
        seed=SAMPLE_SEED,
        source="unseen_question_pool",
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
            "claim_level": "representative score-blind candidate screen",
        },
        "integrity_contract": INTEGRITY_CONTRACT,
        "candidate_configuration": CANDIDATE_CONFIGURATION,
        "decision_rule": DECISION_RULE,
        "exclusion_lineage": exclusion_lineage,
        "selection": selection,
        "frozen_baseline": build_frozen_baseline(
            baseline_run_dirs,
            selected_ids=selection["question_ids_by_domain"],
        ),
        "selected_question_ids_sha256_by_domain": {
            domain: sha256_question_ids(selection["question_ids_by_domain"][domain])
            for domain in DOMAINS
        },
        "excluded_question_ids_sha256_by_domain": {
            domain: sha256_question_ids(selection["excluded_question_ids_by_domain"][domain])
            for domain in DOMAINS
        },
        "source_artifacts": {
            "dataset": {
                name: source_artifact(path)
                for name, path in validation_dataset_paths(questions_path).items()
            },
            "selected_question_images": selected_question_image_sources(
                questions_path,
                selected_ids=selection["question_ids_by_domain"],
            ),
            "causal_plan": source_artifact(causal_plan_path),
            "prior_run_questions": {
                domain: source_artifact(prior_run_questions[domain]) for domain in DOMAINS
            },
            "baseline_run_dirs": {
                domain: {
                    "path": str(baseline_run_dirs[domain]),
                    "files": {
                        name: sha256_file(baseline_run_dirs[domain] / name)
                        for name in BASELINE_RUN_FILES
                    },
                }
                for domain in DOMAINS
            },
        },
    }


def require_validation_slice(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Invalid validation slice schema")
    if manifest.get("integrity_contract") != INTEGRITY_CONTRACT:
        raise ValueError("Validation slice integrity contract changed")
    if manifest.get("candidate_configuration") != CANDIDATE_CONFIGURATION:
        raise ValueError("Validation slice candidate configuration changed")
    if manifest.get("decision_rule") != DECISION_RULE:
        raise ValueError("Validation slice decision rule changed")
    sources = manifest.get("source_artifacts")
    if not isinstance(sources, dict):
        raise TypeError("Validation slice has no source artifacts")
    prior_sources = sources.get("prior_run_questions")
    if not isinstance(prior_sources, dict):
        raise TypeError("Validation slice has no prior-run sources")
    baseline_sources = sources.get("baseline_run_dirs")
    if not isinstance(baseline_sources, dict):
        raise TypeError("Validation slice has no baseline sources")
    dataset_sources = sources.get("dataset")
    if not isinstance(dataset_sources, dict):
        raise TypeError("Validation slice has no dataset sources")
    dataset_paths = {
        name: require_source(dataset_sources.get(name), name=f"dataset.{name}")
        for name in VALIDATION_DATASET_FILES
    }
    source_paths = {
        "questions": dataset_paths["questions_sha256"],
        "causal_plan": require_source(sources.get("causal_plan"), name="causal_plan"),
    }
    prior_paths = {
        domain: require_source(prior_sources.get(domain), name=f"prior_run_questions.{domain}")
        for domain in DOMAINS
    }
    baseline_paths = {
        domain: require_run_dir_source(
            baseline_sources.get(domain),
            name=f"baseline_run_dirs.{domain}",
        )
        for domain in DOMAINS
    }
    rebuilt = _build_validation_slice(
        questions_path=source_paths["questions"],
        causal_plan_path=source_paths["causal_plan"],
        prior_run_questions=prior_paths,
        baseline_run_dirs=baseline_paths,
        created_at=str(manifest.get("created_at") or ""),
    )
    if manifest != rebuilt:
        raise ValueError("Validation slice changed from its score-blind source selection")


def validate_frozen_manifest(
    manifest: dict[str, Any],
    *,
    questions_path: Path,
    require_pinned_baseline: bool = True,
) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Invalid validation slice schema")
    if manifest.get("integrity_contract") != INTEGRITY_CONTRACT:
        raise ValueError("Validation slice integrity contract changed")
    if manifest.get("candidate_configuration") != CANDIDATE_CONFIGURATION:
        raise ValueError("Validation slice candidate configuration changed")
    if manifest.get("decision_rule") != DECISION_RULE:
        raise ValueError("Validation slice decision rule changed")
    sources = manifest.get("source_artifacts")
    lineage = manifest.get("exclusion_lineage")
    if not isinstance(lineage, dict):
        raise TypeError("Validation slice has no exclusion lineage")
    if require_pinned_baseline:
        validate_exclusion_lineage(lineage)
    excluded = exclusion_union(lineage)
    selection = manifest.get("selection")
    if not isinstance(selection, dict):
        raise TypeError("Validation slice has no selection")
    validate_frozen_sources(
        sources,
        questions_path=questions_path,
        selected_ids=selection["question_ids_by_domain"],
        require_pinned_sources=require_pinned_baseline,
    )
    if selection.get("excluded_question_ids_by_domain") != {
        domain: sorted(excluded[domain]) for domain in DOMAINS
    }:
        raise ValueError("Validation slice exclusion lineage changed")
    pool = load_question_metadata(questions_path, excluded=excluded)
    expected = select_stratified_questions(
        pool,
        questions_per_domain=QUESTIONS_PER_DOMAIN,
        seed=SAMPLE_SEED,
        source="unseen_question_pool",
        excluded={domain: sorted(excluded[domain]) for domain in DOMAINS},
    )
    if selection != expected:
        raise ValueError("Validation slice selection changed")
    for domain in DOMAINS:
        selected = selection["question_ids_by_domain"][domain]
        excluded_ids = selection["excluded_question_ids_by_domain"][domain]
        if manifest.get("selected_question_ids_sha256_by_domain", {}).get(domain) != (
            sha256_question_ids(selected)
        ):
            raise ValueError(f"Validation slice {domain} selected hash changed")
        if manifest.get("excluded_question_ids_sha256_by_domain", {}).get(domain) != (
            sha256_question_ids(excluded_ids)
        ):
            raise ValueError(f"Validation slice {domain} excluded hash changed")
    validate_frozen_baseline(
        manifest.get("frozen_baseline"),
        selection=selection,
        require_pinned_sources=require_pinned_baseline,
    )


def validate_frozen_baseline(
    raw: Any,
    *,
    selection: dict[str, Any],
    require_pinned_sources: bool = True,
) -> None:
    if not isinstance(raw, dict):
        raise TypeError("Validation slice has no frozen baseline")
    configuration = raw.get("configuration")
    if not isinstance(configuration, dict) or configuration != {
        "tier": "small",
        "reader_model": CANDIDATE_CONFIGURATION["reader_model"],
        "evaluator_model": CANDIDATE_CONFIGURATION["evaluator_model"],
        "evidence_composition_mode": "reserved_support",
        "source_evidence_bundling": False,
        "sibyl_commit": BASELINE_COMMIT,
    }:
        raise ValueError("Validation slice frozen baseline configuration changed")
    domains = raw.get("domains")
    if not isinstance(domains, dict) or set(domains) != set(DOMAINS):
        raise TypeError("Validation slice frozen baseline domains changed")
    combined_count = 0
    combined_correct = 0
    for domain in DOMAINS:
        selected = selection["question_ids_by_domain"][domain]
        count, correct = validate_frozen_baseline_domain(
            domains[domain],
            domain=domain,
            selected=selected,
            require_pinned_sources=require_pinned_sources,
        )
        combined_count += count
        combined_correct += correct
    expected_combined = {
        "selected_question_count": combined_count,
        "selected_correct": combined_correct,
        "selected_accuracy": combined_correct / combined_count,
    }
    if raw.get("combined") != expected_combined:
        raise ValueError("Validation slice frozen baseline combined summary changed")


def validate_frozen_baseline_domain(
    raw: Any,
    *,
    domain: str,
    selected: list[str],
    require_pinned_sources: bool,
) -> tuple[int, int]:
    if not isinstance(raw, dict):
        raise TypeError(f"Validation slice frozen baseline {domain} is invalid")
    expected_artifact = {
        **BASELINE_ARTIFACTS[domain],
        "run_id": BASELINE_RUN_ID,
        "url": f"https://github.com/hyperb1iss/sibyl/actions/runs/{BASELINE_RUN_ID}",
    }
    if raw.get("artifact") != expected_artifact:
        raise ValueError(f"Validation slice frozen baseline {domain} artifact changed")
    scores = raw.get("selected_scores_by_question_id")
    if (
        not isinstance(scores, dict)
        or list(scores) != sorted(selected)
        or any(not isinstance(score, bool) for score in scores.values())
    ):
        raise ValueError(f"Validation slice frozen baseline {domain} scores changed")
    if require_pinned_sources and sha256_json(scores) != BASELINE_SELECTED_SCORES_SHA256[domain]:
        raise ValueError(f"Validation slice frozen baseline {domain} score source changed")
    correct = sum(scores.values())
    count = len(scores)
    if (
        raw.get("selected_correct") != correct
        or raw.get("selected_question_count") != count
        or float(raw.get("selected_accuracy", -1.0)) != correct / count
    ):
        raise ValueError(f"Validation slice frozen baseline {domain} summary changed")
    if require_pinned_sources:
        expected_full = BASELINE_FULL_RESULTS[domain]
        if (
            raw.get("full_question_count") != expected_full["question_count"]
            or raw.get("full_accuracy") != expected_full["accuracy"]
        ):
            raise ValueError(f"Validation slice frozen baseline {domain} full result changed")
    source_sha256 = raw.get("source_sha256")
    if (
        not isinstance(source_sha256, dict)
        or set(source_sha256) != set(BASELINE_RUN_FILES)
        or any(not is_sha256(value) for value in source_sha256.values())
    ):
        raise ValueError(f"Validation slice frozen baseline {domain} sources changed")
    if require_pinned_sources and source_sha256 != BASELINE_SOURCE_SHA256[domain]:
        raise ValueError(f"Validation slice frozen baseline {domain} source hashes changed")
    return count, correct


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
) -> None:
    actual = {
        "tier": tier,
        "official_harness_commit": OFFICIAL_HARNESS_COMMIT,
        "reader_base_url": reader_base_url,
        "reader_model": reader_model,
        "reader_max_concurrent_requests": reader_max_concurrent_requests,
        "reader_retry_attempts": reader_retry_attempts,
        "evaluator_model": evaluator_model,
        "evidence_composition_mode": evidence_composition_mode,
        "source_evidence_bundling": source_evidence_bundling,
        "include_screenshot_refs": include_screenshot_refs,
    }
    if actual != manifest.get("candidate_configuration"):
        raise ValueError(f"Candidate configuration does not match frozen manifest: {actual}")


def evaluate_candidate(
    manifest: dict[str, Any],
    *,
    run_dirs: dict[str, Path],
    require_pinned_baseline: bool = True,
) -> dict[str, Any]:
    require_domain_paths(run_dirs)
    validate_frozen_baseline(
        manifest.get("frozen_baseline"),
        selection=manifest["selection"],
        require_pinned_sources=require_pinned_baseline,
    )
    baseline = manifest["frozen_baseline"]
    candidate_domains = {}
    sources = {}
    candidate_commit: str | None = None
    for domain in DOMAINS:
        run_dir = run_dirs[domain]
        plan_path = run_dir / "longmemeval_v2_official_plan.json"
        receipt_path = run_dir / "longmemeval_v2_official_receipt.json"
        scores_path = run_dir / "per_question.jsonl"
        plan = load_json(plan_path)
        receipt = load_json(receipt_path)
        expected_ids = manifest["selection"]["question_ids_by_domain"][domain]
        expected_hash = f"sha256:{sha256_question_ids(expected_ids)}"
        if (
            plan.get("domain") != domain
            or plan.get("selected_question_ids_sha256") != expected_hash
            or plan.get("tier") != CANDIDATE_CONFIGURATION["tier"]
            or plan.get("reader_base_url") != CANDIDATE_CONFIGURATION["reader_base_url"]
            or plan.get("reader_model") != CANDIDATE_CONFIGURATION["reader_model"]
            or plan.get("reader_max_concurrent_requests")
            != CANDIDATE_CONFIGURATION["reader_max_concurrent_requests"]
            or plan.get("reader_retry_attempts") != CANDIDATE_CONFIGURATION["reader_retry_attempts"]
            or plan.get("evaluator_model") != CANDIDATE_CONFIGURATION["evaluator_model"]
            or plan.get("evidence_composition_mode")
            != CANDIDATE_CONFIGURATION["evidence_composition_mode"]
            or plan.get("source_evidence_bundling")
            != CANDIDATE_CONFIGURATION["source_evidence_bundling"]
            or plan.get("include_screenshot_refs")
            != CANDIDATE_CONFIGURATION["include_screenshot_refs"]
        ):
            raise ValueError(f"Candidate {domain} run does not match frozen configuration")
        validate_candidate_receipt(
            receipt,
            plan=plan,
            expected_ids=expected_ids,
            domain=domain,
        )
        domain_commit = receipt.get("sibyl_commit")
        if (
            not isinstance(domain_commit, str)
            or len(domain_commit) != GIT_COMMIT_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in domain_commit)
        ):
            raise ValueError(f"Candidate {domain} Sibyl commit is invalid")
        if candidate_commit is None:
            candidate_commit = domain_commit
        elif domain_commit != candidate_commit:
            raise ValueError("Candidate domains have different Sibyl commits")
        scores = question_scores(load_jsonl(scores_path), domain=domain)
        if set(scores) != set(expected_ids):
            raise ValueError(f"Candidate {domain} question set changed")
        correct = sum(scores.values())
        count = len(scores)
        accuracy = correct / count
        baseline_accuracy = baseline["domains"][domain]["selected_accuracy"]
        candidate_domains[domain] = {
            "question_count": count,
            "correct": correct,
            "accuracy": accuracy,
            "baseline_accuracy": baseline_accuracy,
            "accuracy_delta": accuracy - baseline_accuracy,
        }
        sources[domain] = {
            "plan": sha256_file(plan_path),
            "receipt": sha256_file(receipt_path),
            "per_question": sha256_file(scores_path),
        }
    combined_count = sum(candidate_domains[domain]["question_count"] for domain in DOMAINS)
    combined_correct = sum(candidate_domains[domain]["correct"] for domain in DOMAINS)
    combined_accuracy = combined_correct / combined_count
    baseline_accuracy = baseline["combined"]["selected_accuracy"]
    combined_delta = combined_accuracy - baseline_accuracy
    minimum_accuracy_delta = float(DECISION_RULE["minimum_accuracy_delta_over_frozen_baseline"])
    minimum_domain_delta = float(DECISION_RULE["minimum_domain_delta_over_frozen_baseline"])
    passes = combined_delta >= minimum_accuracy_delta and all(
        candidate_domains[domain]["accuracy_delta"] >= minimum_domain_delta for domain in DOMAINS
    )
    return {
        "schema_version": "sibyl-longmemeval-v2-validation-report-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "decision": {
            "outcome": "GO" if passes else "NO-GO",
            "full_run_allowed": passes,
            "checks": {
                "combined_accuracy_delta": combined_delta >= minimum_accuracy_delta,
                "minimum_domain_delta": all(
                    candidate_domains[domain]["accuracy_delta"] >= minimum_domain_delta
                    for domain in DOMAINS
                ),
            },
        },
        "candidate": {
            "sibyl_commit": candidate_commit,
            "domains": candidate_domains,
            "combined": {
                "question_count": combined_count,
                "correct": combined_correct,
                "accuracy": combined_accuracy,
                "baseline_accuracy": baseline_accuracy,
                "accuracy_delta": combined_delta,
            },
        },
        "decision_rule": DECISION_RULE,
        "source_artifacts": sources,
    }


def require_confirmation_selection(causal_plan: dict[str, Any]) -> dict[str, Any]:
    selection = causal_plan.get("confirmation_selection")
    if not isinstance(selection, dict):
        raise TypeError("Causal plan has no confirmation selection")
    for key in ("excluded_question_ids_by_domain", "question_ids_by_domain"):
        values = selection.get(key)
        if not isinstance(values, dict) or set(values) != set(DOMAINS):
            raise TypeError(f"Causal confirmation selection has invalid {key}")
    return selection


def exclusion_union(lineage: dict[str, dict[str, list[str]]]) -> dict[str, set[str]]:
    excluded = {domain: set() for domain in DOMAINS}
    for source, ids_by_domain in lineage.items():
        if set(ids_by_domain) != set(DOMAINS):
            raise ValueError(f"Exclusion lineage {source} has invalid domains")
        for domain in DOMAINS:
            excluded[domain].update(ids_by_domain[domain])
    return excluded


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
                or sha256_question_ids(question_ids) != expected_sha256
            ):
                raise ValueError(f"Validation slice exclusion lineage {source}.{domain} changed")


def validation_dataset_paths(questions_path: Path) -> dict[str, Path]:
    data_root = questions_path.parent
    return {
        name: data_root / relative_path for name, relative_path in VALIDATION_DATASET_FILES.items()
    }


def selected_question_image_sources(
    questions_path: Path,
    *,
    selected_ids: dict[str, list[str]],
) -> dict[str, dict[str, str]]:
    selected = {question_id for domain in DOMAINS for question_id in selected_ids[domain]}
    sources = {}
    for row in load_jsonl(questions_path):
        if not isinstance(row, dict) or row.get("id") not in selected or not row.get("image"):
            continue
        question_id = str(row["id"])
        relative_path = str(row["image"])
        image_path = questions_path.parent / relative_path
        sources[question_id] = {
            "relative_path": relative_path,
            "sha256": sha256_file(image_path),
        }
    return dict(sorted(sources.items()))


def validate_frozen_sources(
    raw: Any,
    *,
    questions_path: Path,
    selected_ids: dict[str, list[str]],
    require_pinned_sources: bool,
) -> None:
    if not isinstance(raw, dict):
        raise TypeError("Validation slice has no source artifacts")
    validate_frozen_dataset_sources(
        raw.get("dataset"),
        questions_path=questions_path,
        require_pinned_sources=require_pinned_sources,
    )
    validate_frozen_exclusion_sources(
        raw,
        require_pinned_sources=require_pinned_sources,
    )
    validate_frozen_question_images(
        raw.get("selected_question_images"),
        questions_path=questions_path,
        selected_ids=selected_ids,
        require_pinned_sources=require_pinned_sources,
    )


def validate_frozen_dataset_sources(
    dataset: Any,
    *,
    questions_path: Path,
    require_pinned_sources: bool,
) -> None:
    if not isinstance(dataset, dict) or set(dataset) != set(VALIDATION_DATASET_FILES):
        raise ValueError("Validation slice dataset sources changed")
    for name, path in validation_dataset_paths(questions_path).items():
        source = dataset.get(name)
        actual_sha256 = sha256_file(path)
        if (
            not isinstance(source, dict)
            or source.get("sha256") != actual_sha256
            or (require_pinned_sources and actual_sha256 != VALIDATION_DATASET_SHA256[name])
        ):
            raise ValueError(f"Validation slice dataset source {name} changed")


def validate_frozen_exclusion_sources(
    raw: dict[str, Any],
    *,
    require_pinned_sources: bool,
) -> None:
    causal_plan = raw.get("causal_plan")
    if not isinstance(causal_plan, dict):
        raise TypeError("Validation slice causal plan source changed")
    if require_pinned_sources:
        if causal_plan.get("sha256") != EXCLUSION_SOURCE_SHA256["causal_plan"]:
            raise ValueError("Validation slice causal plan source changed")
    else:
        require_source(causal_plan, name="causal_plan")
    prior_runs = raw.get("prior_run_questions")
    if not isinstance(prior_runs, dict) or set(prior_runs) != set(DOMAINS):
        raise ValueError("Validation slice prior-run sources changed")
    expected_prior_runs = EXCLUSION_SOURCE_SHA256["prior_run_questions"]
    if not isinstance(expected_prior_runs, dict):
        raise TypeError("Validation slice prior-run source contract is invalid")
    for domain in DOMAINS:
        source = prior_runs.get(domain)
        if not isinstance(source, dict):
            raise TypeError(f"Validation slice prior-run source {domain} changed")
        if require_pinned_sources:
            if source.get("sha256") != expected_prior_runs[domain]:
                raise ValueError(f"Validation slice prior-run source {domain} changed")
        else:
            require_source(source, name=f"prior_run_questions.{domain}")


def validate_frozen_question_images(
    raw: Any,
    *,
    questions_path: Path,
    selected_ids: dict[str, list[str]],
    require_pinned_sources: bool,
) -> None:
    expected = selected_question_image_sources(questions_path, selected_ids=selected_ids)
    if raw != expected:
        raise ValueError("Validation slice selected question images changed")
    if require_pinned_sources:
        pinned = {
            question_id: {
                "relative_path": source["path"],
                "sha256": source["sha256"],
            }
            for question_id, source in SELECTED_QUESTION_IMAGES.items()
        }
        actual = {
            question_id: {
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
            }
            for question_id, source in expected.items()
        }
        if actual != pinned:
            raise ValueError("Validation slice selected question image pins changed")


def validate_candidate_receipt(
    receipt: Any,
    *,
    plan: dict[str, Any],
    expected_ids: list[str],
    domain: str,
) -> None:
    if not isinstance(receipt, dict):
        raise TypeError(f"Candidate {domain} receipt is invalid")
    if {
        "schema_version": receipt.get("schema_version"),
        "suite": receipt.get("suite"),
        "suite_version": receipt.get("suite_version"),
        "method": receipt.get("method"),
        "domain": receipt.get("domain"),
        "tier": receipt.get("tier"),
    } != {
        "schema_version": "sibyl-longmemeval-v2-official-receipt-v1",
        "suite": "LongMemEval-V2 official",
        "suite_version": "official-harness-v1",
        "method": "sibyl_live_api",
        "domain": domain,
        "tier": CANDIDATE_CONFIGURATION["tier"],
    }:
        raise ValueError(f"Candidate {domain} receipt identity changed")
    validate_candidate_harness_receipt(receipt, domain=domain)
    validate_candidate_model_receipt(receipt, domain=domain)
    validate_candidate_runtime_receipt(receipt, plan=plan, domain=domain)
    validate_candidate_checks(receipt, domain=domain)
    validate_candidate_dataset_receipt(receipt, expected_ids=expected_ids, domain=domain)


def validate_candidate_harness_receipt(receipt: dict[str, Any], *, domain: str) -> None:
    official_repo = receipt.get("official_repo")
    if not isinstance(official_repo, dict) or {
        "url": official_repo.get("url"),
        "commit": official_repo.get("commit"),
        "harness_path": official_repo.get("harness_path"),
        "harness_exists": official_repo.get("harness_exists"),
    } != {
        "url": "https://github.com/xiaowu0162/LongMemEval-V2",
        "commit": OFFICIAL_HARNESS_COMMIT,
        "harness_path": "evaluation/harness.py",
        "harness_exists": True,
    }:
        raise ValueError(f"Candidate {domain} official harness changed")


def validate_candidate_model_receipt(receipt: dict[str, Any], *, domain: str) -> None:
    models = receipt.get("models")
    if not isinstance(models, dict) or {
        "reader_model": models.get("reader_model"),
        "reader_base_url": str(models.get("reader_base_url") or "").rstrip("/"),
        "evaluator_model": models.get("evaluator_model"),
    } != {
        "reader_model": CANDIDATE_CONFIGURATION["reader_model"],
        "reader_base_url": CANDIDATE_CONFIGURATION["reader_base_url"],
        "evaluator_model": CANDIDATE_CONFIGURATION["evaluator_model"],
    }:
        raise ValueError(f"Candidate {domain} model receipt changed")


def validate_candidate_runtime_receipt(
    receipt: dict[str, Any],
    *,
    plan: dict[str, Any],
    domain: str,
) -> None:
    provenance = receipt.get("runner_provenance")
    plan_provenance = plan.get("runner_provenance")
    if (
        not isinstance(provenance, dict)
        or not isinstance(plan_provenance, dict)
        or provenance.get("git_dirty") is not False
        or provenance.get("git_status") != "clean"
        or receipt.get("sibyl_commit") != provenance.get("sibyl_commit")
        or provenance.get("sibyl_commit") != plan_provenance.get("sibyl_commit")
    ):
        raise ValueError(f"Candidate {domain} runtime provenance changed")


def validate_candidate_checks(receipt: dict[str, Any], *, domain: str) -> None:
    checks = receipt.get("checks")
    if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
        raise TypeError(f"Candidate {domain} receipt checks are invalid")
    actual = {str(check.get("name")): check.get("status") for check in checks}
    if len(actual) != len(checks) or actual != RECEIPT_CHECK_STATUSES:
        raise ValueError(f"Candidate {domain} receipt checks changed")


def validate_candidate_dataset_receipt(
    receipt: dict[str, Any],
    *,
    expected_ids: list[str],
    domain: str,
) -> None:
    dataset = receipt.get("dataset")
    expected = {
        **VALIDATION_DATASET_SHA256,
        "name": "longmemeval-v2",
        "tier": CANDIDATE_CONFIGURATION["tier"],
        "question_count": len(expected_ids),
        "selected_question_ids_sha256": f"sha256:{sha256_question_ids(expected_ids)}",
    }
    if not isinstance(dataset, dict) or any(
        dataset.get(key) != value for key, value in expected.items()
    ):
        raise ValueError(f"Candidate {domain} dataset receipt changed")


def build_frozen_baseline(
    run_dirs: dict[str, Path],
    *,
    selected_ids: dict[str, list[str]],
) -> dict[str, Any]:
    domains = {}
    for domain in DOMAINS:
        run_dir = run_dirs[domain]
        plan = load_json(run_dir / "longmemeval_v2_official_plan.json")
        metrics = load_json(run_dir / "aggregated_metrics.json")
        rows = load_jsonl(run_dir / "per_question.jsonl")
        if (
            plan.get("domain") != domain
            or plan.get("runner_provenance", {}).get("sibyl_commit") != BASELINE_COMMIT
            or plan.get("reader_model") != CANDIDATE_CONFIGURATION["reader_model"]
            or plan.get("evaluator_model") != CANDIDATE_CONFIGURATION["evaluator_model"]
        ):
            raise ValueError(f"Frozen baseline {domain} has unexpected run identity")
        scores = question_scores(rows, domain=domain)
        requested = selected_ids[domain]
        missing = sorted(set(requested) - set(scores))
        if missing:
            raise ValueError(f"Frozen baseline {domain} is missing questions: {missing}")
        selected_scores = {question_id: scores[question_id] for question_id in sorted(requested)}
        selected_correct = sum(selected_scores.values())
        overall = metrics.get("overall")
        if not isinstance(overall, dict):
            raise TypeError(f"Frozen baseline {domain} has no aggregate metrics")
        domains[domain] = {
            "artifact": {
                **BASELINE_ARTIFACTS[domain],
                "run_id": BASELINE_RUN_ID,
                "url": f"https://github.com/hyperb1iss/sibyl/actions/runs/{BASELINE_RUN_ID}",
            },
            "full_question_count": int(overall["count_all_questions"]),
            "full_accuracy": float(overall["overall_full_set"]),
            "selected_question_count": len(selected_scores),
            "selected_correct": selected_correct,
            "selected_accuracy": selected_correct / len(selected_scores),
            "selected_scores_by_question_id": selected_scores,
            "source_sha256": {name: sha256_file(run_dir / name) for name in BASELINE_RUN_FILES},
        }
    combined_count = sum(domains[domain]["selected_question_count"] for domain in DOMAINS)
    combined_correct = sum(domains[domain]["selected_correct"] for domain in DOMAINS)
    return {
        "configuration": {
            "tier": "small",
            "reader_model": CANDIDATE_CONFIGURATION["reader_model"],
            "evaluator_model": CANDIDATE_CONFIGURATION["evaluator_model"],
            "evidence_composition_mode": "reserved_support",
            "source_evidence_bundling": False,
            "sibyl_commit": BASELINE_COMMIT,
        },
        "domains": domains,
        "combined": {
            "selected_question_count": combined_count,
            "selected_correct": combined_correct,
            "selected_accuracy": combined_correct / combined_count,
        },
    }


def question_scores(rows: list[Any], *, domain: str) -> dict[str, bool]:
    scores = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError(f"{domain} has a non-object score row")
        question_id = str(row.get("question_id") or "")
        score = row.get("score_bool")
        if not question_id or question_id in scores or not isinstance(score, bool):
            raise ValueError(f"{domain} has invalid score identity")
        scores[question_id] = score
    return scores


def is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == SHA256_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in digest
    )


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_question_metadata(
    questions_path: Path,
    *,
    excluded: dict[str, set[str]],
) -> dict[str, dict[str, str]]:
    pool: dict[str, dict[str, str]] = {domain: {} for domain in DOMAINS}
    observed_ids: set[str] = set()
    observed_by_domain: dict[str, set[str]] = {domain: set() for domain in DOMAINS}
    with questions_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise TypeError(f"Expected JSON object at {questions_path}:{line_number}")
            question_id = str(raw.get("id") or "")
            domain = str(raw.get("domain") or "")
            question_type = str(raw.get("question_type") or "")
            if not question_id or question_id in observed_ids:
                raise ValueError(f"Question metadata has an invalid id: {question_id!r}")
            observed_ids.add(question_id)
            if domain in DOMAINS:
                observed_by_domain[domain].add(question_id)
            if domain in DOMAINS and question_id not in excluded[domain]:
                if not question_type:
                    raise ValueError(f"Question {question_id!r} has no question_type")
                pool[domain][question_id] = question_type
    missing = {domain: sorted(excluded[domain] - observed_by_domain[domain]) for domain in DOMAINS}
    if any(missing.values()):
        raise ValueError(f"Excluded validation questions are missing from their domain: {missing}")
    return pool


def load_prior_question_ids(path: Path, *, domain: str) -> set[str]:
    rows = load_json(path)
    if not isinstance(rows, list) or not rows:
        raise TypeError(f"Prior {domain} question input must be a non-empty list")
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError(f"Prior {domain} question input has a non-object row")
        row_domain = str(row.get("domain") or domain)
        question_id = str(row.get("id") or "")
        if row_domain != domain or not question_id or question_id in ids:
            raise ValueError(f"Prior {domain} question input has invalid identity")
        ids.add(question_id)
    return ids


def require_domain_paths(paths: dict[str, Path]) -> None:
    if set(paths) != set(DOMAINS):
        raise ValueError(f"Expected prior-run question paths for {DOMAINS}")


def source_artifact(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def require_source(raw: Any, *, name: str) -> Path:
    if not isinstance(raw, dict):
        raise TypeError(f"Validation slice source {name} is missing")
    path = Path(str(raw.get("path") or ""))
    if not path.is_file() or raw.get("sha256") != sha256_file(path):
        raise ValueError(f"Validation slice source {name} changed")
    return path


def require_run_dir_source(raw: Any, *, name: str) -> Path:
    if not isinstance(raw, dict):
        raise TypeError(f"Validation slice source {name} is missing")
    path = Path(str(raw.get("path") or ""))
    files = raw.get("files")
    if not isinstance(files, dict) or set(files) != set(BASELINE_RUN_FILES):
        raise ValueError(f"Validation slice source {name} has invalid files")
    if any(files[file_name] != sha256_file(path / file_name) for file_name in BASELINE_RUN_FILES):
        raise ValueError(f"Validation slice source {name} changed")
    return path


def sha256_question_ids(question_ids: list[str]) -> str:
    encoded = json.dumps(sorted(question_ids), separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def parse_prior_run_questions(values: list[str]) -> dict[str, Path]:
    return parse_domain_paths(values, option="--prior-run-questions")


def parse_domain_paths(values: list[str], *, option: str) -> dict[str, Path]:
    result = {}
    for value in values:
        domain, separator, raw_path = value.partition("=")
        if not separator or domain not in DOMAINS or domain in result or not raw_path:
            raise ValueError(f"Invalid {option} value: {value!r}")
        result[domain] = Path(raw_path)
    require_domain_paths(result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--questions", required=True)
    generate.add_argument("--causal-plan", required=True)
    generate.add_argument(
        "--prior-run-questions",
        action="append",
        required=True,
        metavar="DOMAIN=PATH",
    )
    generate.add_argument(
        "--baseline-run",
        action="append",
        required=True,
        metavar="DOMAIN=DIR",
    )
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
    verify.add_argument("--output", required=True)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--manifest", required=True)
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
            causal_plan_path=Path(args.causal_plan),
            prior_run_questions=parse_prior_run_questions(args.prior_run_questions),
            baseline_run_dirs=parse_domain_paths(args.baseline_run, option="--baseline-run"),
        )
    elif args.command == "verify":
        manifest = load_json(Path(args.manifest))
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
            load_json(Path(args.manifest)),
            run_dirs=parse_domain_paths(args.run, option="--run"),
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
