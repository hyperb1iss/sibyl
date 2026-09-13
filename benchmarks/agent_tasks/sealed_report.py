"""Authenticate a frozen final schedule and retain paired cluster uncertainty.

This report is a statistical foundation, not a curator, transport broker, or
VM isolation verifier. Caller-owned schedule and analysis pins precede release.
"""

from __future__ import annotations

import base64
import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any

from benchmarks.agent_tasks.manifest import ManifestError, canonical_bytes, digest, identity
from benchmarks.agent_tasks.sealed_evaluator import _authenticate
from benchmarks.agent_tasks.transfer_report import ARMS, COMMON_FIELDS, IDENTITY_FIELDS
from benchmarks.longmemeval_v2_reader_replication_report import percentile
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from sibyl_core.tasks.eval_receipts import TaskAssignment, assignment_digest, verify_outcome

CHECKPOINTS = (0, 1, 3, 10)
PRIMARY_CHECKPOINT = 10
FAMILYWISE_ALPHA = 0.05
SHA256_LENGTH = 64
MIN_BOOTSTRAP_SAMPLES = 2
CATALOG_DOMAIN = b"sibyl-sealed-terminal-catalog-v1\x00"
BINDINGS = ("curator_commitment", "isolation_qualification", "pack_receipt", "source_policy")
FAILURES = {
    "missing",
    "abstained_pack",
    "transport",
    "cleanup",
    "timeout",
    "budget",
    "grader",
    "unknown",
}


@dataclass(frozen=True)
class SealedCell:
    """A curator-clustered task cell committed before any outcome is exposed."""

    assignment: TaskAssignment
    cluster: str
    category: str
    repetition: int
    expected: Mapping[str, Any]
    bindings: Mapping[str, str]

    def public_identity(self) -> dict[str, Any]:
        return {**asdict(self), "assignment": self.assignment.model_dump(mode="json")}


@dataclass(frozen=True)
class Analysis:
    bootstrap_seed: int
    bootstrap_samples: int
    weighting: str = "equal_independent_cluster"
    primary_checkpoint: int = 10
    primary_control: str = "simple_summary"
    familywise_alpha: float = 0.05


@dataclass(frozen=True)
class OutcomeEvidence:
    receipt: bytes
    outcome: bytes
    transcript: bytes
    audit: bytes


def schedule_digest(schedule: Sequence[SealedCell]) -> str:
    return identity([cell.public_identity() for cell in schedule])


def sign_catalog(payload: Mapping[str, Any], key: Ed25519PrivateKey) -> bytes:
    """Sign only in the trusted finalizer after the complete terminal inventory."""
    return canonical_bytes(
        {
            "payload": dict(payload),
            "signature": base64.b64encode(
                key.sign(CATALOG_DOMAIN + canonical_bytes(payload))
            ).decode(),
        }
    )


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_LENGTH
        and all(c in "0123456789abcdef" for c in value)
    )


def _schedule(schedule: Sequence[SealedCell], analysis: Analysis) -> None:
    _require(
        type(analysis.bootstrap_seed) is int
        and analysis.bootstrap_seed >= 0
        and type(analysis.bootstrap_samples) is int
        and analysis.bootstrap_samples >= MIN_BOOTSTRAP_SAMPLES,
        "bootstrap seed and sample count must be preregistered",
    )
    _require(
        analysis.weighting == "equal_independent_cluster"
        and analysis.primary_checkpoint == PRIMARY_CHECKPOINT
        and analysis.primary_control == "simple_summary"
        and analysis.familywise_alpha == FAMILYWISE_ALPHA,
        "unsupported sealed analysis contract",
    )
    _require(
        schedule and len({c.assignment.attempt_id for c in schedule}) == len(schedule),
        "empty schedule or duplicate physical assignment",
    )
    pairs: dict[tuple[str, int], list[SealedCell]] = defaultdict(list)
    tasks: dict[str, Any] = {}
    repetitions: dict[str, set[int]] = defaultdict(set)
    packs: dict[tuple[int, str], Any] = {}
    policy = None
    checkpoint_revisions: dict[int, str] = {}
    for cell in schedule:
        a, e = cell.assignment, cell.expected
        _require(
            a.split == "sealed"
            and a.arm_id in ARMS
            and a.checkpoint in CHECKPOINTS
            and cell.cluster
            and cell.category in {"related_transfer", "applicability_contrast"}
            and type(cell.repetition) is int
            and cell.repetition >= 0,
            "invalid sealed cell",
        )
        _require(
            set(e) == set(IDENTITY_FIELDS)
            and set(cell.bindings) == set(BINDINGS)
            and all(_sha(v) for v in cell.bindings.values()),
            "incomplete paired identity",
        )
        for name, value in {
            "experiment_id": a.experiment_id,
            "task_id": a.task_id,
            "task_family_id": a.family_id,
            "task_sha256": a.task_sha256,
            "arm_id": a.arm_id,
            "memory_pack_sha256": a.memory_pack_sha256,
            "seed": a.seed,
        }.items():
            _require(e[name] == value, "assignment and scheduled identity disagree")
        _require(
            a.checkpoint not in checkpoint_revisions
            or checkpoint_revisions[a.checkpoint] == a.experiment_revision,
            "paired checkpoint experiment revision differs",
        )
        checkpoint_revisions[a.checkpoint] = a.experiment_revision
        current = (
            {k: e[k] for k in COMMON_FIELDS},
            a.organization_id,
            a.owner_principal_id,
            a.experiment_id,
            a.controller_policy_sha256,
            a.evaluator_sha256,
            a.runtime_sha256,
            a.image,
            cell.bindings["curator_commitment"],
            cell.bindings["isolation_qualification"],
        )
        _require(
            policy is None or policy == current, "foreign solver/evaluator policy or commitment"
        )
        policy = current
        task = (
            cell.cluster,
            cell.category,
            a.family_id,
            a.task_sha256,
            a.task_revision,
            a.checker_sha256,
            a.oracle_sha256,
        )
        _require(
            a.task_id not in tasks or tasks[a.task_id] == task, "task or cluster membership drift"
        )
        tasks[a.task_id] = task
        pack_key = (a.checkpoint, a.arm_id)
        pack = (
            a.memory_pack_sha256,
            e["arm_sha256"],
            cell.bindings["pack_receipt"],
            cell.bindings["source_policy"],
        )
        _require(pack_key not in packs or packs[pack_key] == pack, "checkpoint pack policy drift")
        packs[pack_key] = pack
        pairs[(a.task_id, cell.repetition)].append(cell)
        repetitions[a.task_id].add(cell.repetition)
    _require(len({tuple(sorted(r)) for r in repetitions.values()}) == 1, "unequal task repetitions")
    for cells in pairs.values():
        _require(
            Counter((c.assignment.checkpoint, c.assignment.arm_id) for c in cells)
            == Counter((checkpoint, arm) for checkpoint in CHECKPOINTS for arm in ARMS),
            "each task repetition requires the complete sixteen-cell schedule",
        )
        _require(len({c.assignment.seed for c in cells}) == 1, "unpaired repetition seed")
        for checkpoint in CHECKPOINTS:
            rows = [c for c in cells if c.assignment.checkpoint == checkpoint]
            _require(
                len({c.expected["manifest_sha256"] for c in rows}) == 1,
                "paired manifest identity differs",
            )
            _require(
                len({c.bindings["source_policy"] for c in rows}) == 1,
                "arms do not share checkpoint source policy",
            )


def joint_intervals(vectors: Mapping[str, Sequence[float]], analysis: Analysis) -> dict[str, Any]:
    """Draw one cluster-index schedule for every contrast, never marginal schedules."""
    counts = {len(values) for values in vectors.values()}
    _require(len(counts) == 1 and next(iter(counts), 0) > 0, "empty or unpaired cluster vectors")
    _require(
        all(math.isfinite(x) for values in vectors.values() for x in values), "nonfinite effect"
    )
    count = next(iter(counts))
    generator = random.Random(analysis.bootstrap_seed)  # noqa: S311
    samples: dict[str, list[float]] = {key: [] for key in vectors}
    for _ in range(analysis.bootstrap_samples):
        indices = generator.choices(range(count), k=count)
        for key, values in vectors.items():
            samples[key].append(mean(values[index] for index in indices))
    result = {}
    q = analysis.familywise_alpha / (2 * 3)
    for key, values in vectors.items():
        ordered = sorted(samples[key])
        result[key] = {
            "estimate": mean(values),
            "cluster_count": count,
            "unadjusted_95": [percentile(ordered, 0.025), percentile(ordered, 0.975)],
            "bonferroni_three_95": [percentile(ordered, q), percentile(ordered, 1 - q)],
        }
    return {
        "contrasts": result,
        "samples": analysis.bootstrap_samples,
        "seed": analysis.bootstrap_seed,
        "joint_cluster_indices": True,
        "simultaneous_quantiles": [q, 1 - q],
        "coverage": "nominal percentile bootstrap; no finite-sample coverage guarantee",
    }


def _category_rates(schedule, rows):
    categories = {}
    for category in sorted({c.category for c in schedule}):
        selected = [c for c in schedule if c.category == category]
        selected_clusters = sorted({c.cluster for c in selected})
        category_rates = {}
        for cluster in selected_clusters:
            subset = [r for r in rows if r["cluster"] == cluster and r["category"] == category]
            category_rates[cluster] = {
                str(checkpoint): {
                    arm: mean(
                        float(r["success"])
                        for r in subset
                        if r["checkpoint"] == checkpoint and r["arm"] == arm
                    )
                    for arm in ARMS
                }
                for checkpoint in CHECKPOINTS
            }
        categories[category] = category_rates
    return categories


def _paired_intervals(rates, clusters, analysis):
    vectors = {
        f"{checkpoint}:{control}": [
            rates[c][checkpoint]["sibyl_consolidation"] - rates[c][checkpoint][control]
            for c in clusters
        ]
        for checkpoint in CHECKPOINTS
        for control in ARMS[:-1]
    }
    vectors.update(
        {
            f"change:{control}:{checkpoint}": [
                vectors[f"{checkpoint}:{control}"][i] - vectors[f"0:{control}"][i]
                for i in range(len(clusters))
            ]
            for checkpoint in CHECKPOINTS[1:]
            for control in ARMS[:-1]
        }
    )
    intervals = joint_intervals(vectors, analysis)
    primary = intervals["contrasts"]["10:simple_summary"]
    release = {control: intervals["contrasts"][f"10:{control}"] for control in ARMS[:-1]}
    return intervals, primary, release


def summarize_sealed(
    schedule: Sequence[SealedCell],
    analysis: Analysis,
    catalog: bytes,
    outcomes: Mapping[str, OutcomeEvidence],
    events: Mapping[str, bytes],
    *,
    expected_schedule_sha256: str,
    expected_analysis_sha256: str,
    trusted_catalog_key: Ed25519PublicKey,
    trusted_outcome_key: Ed25519PublicKey,
    trusted_issuer_id: str,
) -> dict[str, Any]:
    """Require complete signed terminal accounting before exposing final aggregates."""
    _schedule(schedule, analysis)
    schedule_sha = schedule_digest(schedule)
    _require(
        schedule_sha == expected_schedule_sha256
        and identity(asdict(analysis)) == expected_analysis_sha256,
        "schedule or analysis differs from preregistration",
    )
    payload = _authenticate(catalog, CATALOG_DOMAIN, trusted_catalog_key)
    _require(
        set(payload)
        == {"schema_version", "issuer_id", "schedule_sha256", "analysis_sha256", "cells"}
        and payload["schema_version"] == "sibyl-sealed-terminal-catalog-v1"
        and payload["issuer_id"] == trusted_issuer_id
        and payload["schedule_sha256"] == schedule_sha
        and payload["analysis_sha256"] == expected_analysis_sha256,
        "foreign terminal catalog",
    )
    records = payload["cells"]
    expected_ids = {c.assignment.attempt_id for c in schedule}
    _require(
        isinstance(records, dict) and set(records) == expected_ids,
        "terminal catalog must account for every scheduled cell",
    )
    _require(set(outcomes) <= expected_ids and set(events) <= expected_ids, "unscheduled evidence")
    rows = []
    physical: set[str] = set()
    receipt_hashes: set[str] = set()
    used_outcomes: set[str] = set()
    used_events: set[str] = set()
    grouped: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    for cell in schedule:
        a = cell.assignment
        record = records[a.attempt_id]
        _require(
            isinstance(record, dict)
            and set(record)
            == {
                "assignment_sha256",
                "status",
                "physical_ids",
                "evidence_sha256",
                "snapshot_sha256",
                "budget_status",
                "cost_usd",
                "cleanup_status",
            },
            "malformed terminal record",
        )
        _require(
            record["assignment_sha256"] == assignment_digest(a), "terminal assignment mismatch"
        )
        ids = record["physical_ids"]
        _require(
            isinstance(ids, list)
            and all(isinstance(i, str) and i for i in ids)
            and len(set(ids)) == len(ids)
            and not physical.intersection(ids),
            "duplicate physical attempt",
        )
        physical.update(ids)
        _require(
            record["budget_status"] in {"verified_within_budget", "exceeded", "unknown"}
            and record["cleanup_status"] in {"verified", "failed", "unknown"},
            "invalid terminal qualification",
        )
        cost = record["cost_usd"]
        _require(
            cost is None or (type(cost) in (int, float) and math.isfinite(cost) and cost >= 0),
            "invalid authoritative cost",
        )
        success = False
        status = record["status"]
        if status == "scored":
            _require(
                a.attempt_id in outcomes and ids, "scored cell lacks evidence or physical attempts"
            )
            evidence = outcomes[a.attempt_id]
            receipt_hash = digest(evidence.receipt)
            _require(
                receipt_hash == record["evidence_sha256"] and receipt_hash not in receipt_hashes,
                "outcome receipt reused or changed",
            )
            receipt_hashes.add(receipt_hash)
            verified = verify_outcome(
                evidence.receipt,
                trusted_public_key=trusted_outcome_key,
                trusted_issuer_id=trusted_issuer_id,
                expected_assignment=a,
                expected_controller_policy_sha256=a.controller_policy_sha256,
                outcome_bytes=evidence.outcome,
                transcript_bytes=evidence.transcript,
                episode_bytes=evidence.audit,
            )
            _require(
                verified.outcome.snapshot_sha256 == record["snapshot_sha256"],
                "submission snapshot differs",
            )
            status = verified.outcome.status
            success = (
                verified.outcome.success
                and record["budget_status"] == "verified_within_budget"
                and record["cleanup_status"] == "verified"
            )
            used_outcomes.add(a.attempt_id)
        else:
            _require(
                status in FAILURES
                and a.attempt_id in events
                and events[a.attempt_id]
                and digest(events[a.attempt_id]) == record["evidence_sha256"],
                "terminal failure lacks authenticated retained event",
            )
            used_events.add(a.attempt_id)
        grouped[(cell.cluster, a.checkpoint, a.arm_id)].append(float(success))
        rows.append(
            {
                "attempt_id": a.attempt_id,
                "physical_ids": ids,
                "evidence_sha256": record["evidence_sha256"],
                "cluster": cell.cluster,
                "category": cell.category,
                "checkpoint": a.checkpoint,
                "arm": a.arm_id,
                "status": status,
                "success": success,
                "budget_status": record["budget_status"],
                "cleanup_status": record["cleanup_status"],
                "cost_usd": cost,
            }
        )
    _require(
        used_outcomes == set(outcomes) and used_events == set(events),
        "unused or substituted evidence",
    )
    clusters = sorted({c.cluster for c in schedule})
    rates = {
        cluster: {
            checkpoint: {arm: mean(grouped[(cluster, checkpoint, arm)]) for arm in ARMS}
            for checkpoint in CHECKPOINTS
        }
        for cluster in clusters
    }
    intervals, primary, release = _paired_intervals(rates, clusters, analysis)
    categories = _category_rates(schedule, rows)
    return {
        "schema_version": "sibyl-sealed-report-foundation-v1",
        "schedule_sha256": schedule_sha,
        "analysis_sha256": expected_analysis_sha256,
        "catalog_sha256": digest(catalog),
        "complete_terminal_catalog": True,
        "scheduled_cells": len(schedule),
        "cells": rows,
        "terminal_statuses": dict(Counter(row["status"] for row in rows)),
        "unknown_cost_cells": sum(row["cost_usd"] is None for row in rows),
        "known_execution_cost_usd": sum(row["cost_usd"] or 0 for row in rows),
        "cost_scope": "signed execution costs; unknown and preparation costs remain separate",
        "cluster_rates": rates,
        "category_cluster_rates": categories,
        "primary_summary_contrast": {
            "estimate": primary["estimate"],
            "interval_95": primary["unadjusted_95"],
        },
        "release_contrasts": release,
        "statistical_release_gate": all(r["bonferroni_three_95"][0] > 0 for r in release.values()),
        "exploratory": {
            k: {"estimate": v["estimate"], "interval_95": v["unadjusted_95"]}
            for k, v in intervals["contrasts"].items()
            if not k.startswith("10:")
        },
        "inference": {k: v for k, v in intervals.items() if k != "contrasts"},
        "release_ready": False,
        "remaining_prerequisites": [
            "curator and cohort/power acceptance",
            "current VM isolation",
            "trusted release/executor",
            "external transport budget proof",
            "preparation costs and separate trust gates",
        ],
    }
