"""Synthetic fixtures routed through the product's source-support preparation."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sibyl_core.ai.decisions import DecisionRequest
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.models.reflection import ClaimRecord, ReflectionCandidate
from sibyl_core.tasks import memory_validation, source_support
from sibyl_core.tasks._evidence_json import read_json_value
from sibyl_core.tasks.episode_evidence import EvidenceCitation
from sibyl_core.tasks.memory_validation import OriginalValidationEvidence
from sibyl_core.tasks.procedure_review import review_digest

LABELS = frozenset(option.label for option in source_support.OPTIONS)
ORG = "synthetic-source-support"
VERSION = "jev-product-source-support-study-v1"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = read_json_value(path.read_bytes())
    if not isinstance(cases, list) or not cases:
        raise ValueError("support cases must be a nonempty list")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise TypeError("support case must be an object")
        for field in ("id", "category", "content", "rationale"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError("support case requires nonempty text fields")
        if case["id"] in seen:
            raise ValueError("duplicate support case id")
        seen.add(case["id"])
        claims = case.get("claims")
        if not isinstance(claims, list) or any(
            not isinstance(claim, str) or not claim.strip() for claim in claims
        ):
            raise ValueError("support claims must be nonempty strings")
        paths = {"/content", *(f"/claim_records/{i}/content" for i in range(len(claims)))}
        expected = case.get("expected")
        if (
            not isinstance(expected, dict)
            or set(expected) != paths
            or any(value not in LABELS for value in expected.values())
        ):
            raise ValueError("support gold must cover every assertion exactly")
        sources = case.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("support cases require original sources")
        ids = set()
        for source in sources:
            if not isinstance(source, dict) or any(
                not isinstance(source.get(key), str) or not source[key].strip()
                for key in ("id", "text")
            ):
                raise ValueError("support sources require text identity and content")
            if source.get("provenance") not in {"reported", "signed"} or source["id"] in ids:
                raise ValueError("support sources require unique ids and explicit provenance")
            ids.add(source["id"])
    return cases


def make_request(case: dict[str, Any], run_id: str) -> DecisionRequest:
    """Use caller-supplied synthetic authority; never open a graph or publish a claim."""
    observations = []
    evidence = []
    for source in case["sources"]:
        content = source["text"].encode()
        observation = SourceObservation(
            SourceIdentity(ORG, SourceKind.RAW_CAPTURE, source["id"]),
            generation=1,
            content_sha256=hashlib.sha256(content).hexdigest(),
            revision=1,
            durable=True,
            incarnation="synthetic-first",
        )
        observations.append(observation)
        evidence.append(
            OriginalValidationEvidence(
                source["id"], content, review_digest(asdict(observation)), source["provenance"]
            )
        )
    source_ids = [source["id"] for source in case["sources"]]
    candidate = ReflectionCandidate(
        "claim",
        "Synthetic candidate",
        case["content"],
        "Source-support qualification",
        0.5,
        raw_source_ids=source_ids,
        claim_records=[
            ClaimRecord(
                text,
                source_ids,
                0.5,
                id=f"claim-{index}",
                created_at="2026-01-01T00:00:00+00:00",
            )
            for index, text in enumerate(case["claims"])
        ],
    )
    prepared = memory_validation.prepare_reflection_validation(
        candidate,
        parent_operation_id=review_digest(run_id),
        parent_candidate_sha256=review_digest(candidate.to_dict()),
        evidence=evidence,
        citations={
            f"s{index}": EvidenceCitation(item.source_id, ((0, len(item.content)),))
            for index, item in enumerate(evidence)
        },
    )
    route = OpenRouterDecisionRoute()
    return source_support.prepare_source_support(
        prepared,
        observations=tuple(observations),
        original_evidence=tuple(evidence),
        candidate_id=case["id"],
        org_id=ORG,
        project_id=None,
        authorized_view_fingerprint=review_digest("synthetic-public-fixtures"),
        requested_model_id=route.requested_model_id,
        provider_route_id=route.route_id,
        route_policy_sha256=route.policy_sha256,
        request_id=run_id,
        caller_policy_version=VERSION,
        policy_epoch=0,
    )


def program_hashes() -> dict[str, str]:
    """Pin adapter and both product preparation owners used to construct requests."""
    paths = [Path(__file__), Path(memory_validation.__file__), Path(source_support.__file__)]
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
