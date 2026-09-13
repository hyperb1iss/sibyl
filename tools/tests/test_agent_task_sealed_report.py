"""Synthetic paired outcomes only; no sealed corpus, services, or provider calls."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

import pytest
from benchmarks.agent_tasks.manifest import canonical_bytes, digest, identity
from benchmarks.agent_tasks.sealed_report import (
    ARMS,
    CHECKPOINTS,
    Analysis,
    OutcomeEvidence,
    SealedCell,
    joint_intervals,
    schedule_digest,
    sign_catalog,
    summarize_sealed,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sibyl_core.tasks.eval_receipts import TaskAssignment, assignment_digest, sign_outcome

CLUSTER_COUNT = 3
RELATED_CLUSTERS = 2
CELL_COUNT = CLUSTER_COUNT * len(CHECKPOINTS) * len(ARMS)
SIBYL_CELL_COUNT = CLUSTER_COUNT * len(CHECKPOINTS)


@pytest.fixture
def sealed_report_case():
    key = Ed25519PrivateKey.generate()
    analysis = Analysis(32, 200)
    cells, outcomes, records = [], {}, {}
    for task in range(CLUSTER_COUNT):
        for checkpoint in CHECKPOINTS:
            for arm in ARMS:
                attempt = f"{len(cells):032x}"
                pack = identity([checkpoint, arm])
                assignment = TaskAssignment(
                    organization_id="org",
                    owner_principal_id="owner",
                    experiment_id="synthetic",
                    experiment_revision="v1",
                    task_id=f"task-{task}",
                    task_revision="v1",
                    task_sha256=identity(task),
                    family_id=f"family-{task}",
                    split="sealed",
                    arm_id=arm,
                    checkpoint=checkpoint,
                    seed=3,
                    memory_pack_sha256=pack,
                    controller_policy_sha256="1" * 64,
                    checker_sha256="2" * 64,
                    oracle_sha256="3" * 64,
                    evaluator_sha256="4" * 64,
                    runtime_sha256="5" * 64,
                    image="sha256:" + "6" * 64,
                    attempt_id=attempt,
                )
                expected = {
                    "experiment_id": "synthetic",
                    "manifest_sha256": "7" * 64,
                    "task_id": assignment.task_id,
                    "task_family_id": assignment.family_id,
                    "task_sha256": assignment.task_sha256,
                    "arm_id": arm,
                    "arm_sha256": pack,
                    "memory_pack_sha256": pack,
                    "seed": 3,
                    "controller_model": "model",
                    "controller_tools": ["shell"],
                    "controller_budget": {"tokens": 1000},
                    "runtime": {"pin": "8" * 64},
                    "runner_source_sha256": "9" * 64,
                }
                cells.append(
                    SealedCell(
                        assignment,
                        f"cluster-{task}",
                        "related_transfer" if task < RELATED_CLUSTERS else "applicability_contrast",
                        0,
                        expected,
                        {
                            "curator_commitment": "a" * 64,
                            "isolation_qualification": "b" * 64,
                            "pack_receipt": pack,
                            "source_policy": identity(checkpoint),
                        },
                    )
                )
                passed = arm == "sibyl_consolidation"
                outcome = canonical_bytes(
                    {
                        "schema_version": "sibyl-json-cli-outcome-v1",
                        "attempt_id": attempt,
                        "status": "passed" if passed else "task_failed",
                        "passed": passed,
                        "snapshot_sha256": identity(attempt),
                        **{
                            field: getattr(assignment, field)
                            for field in [
                                "checker_sha256",
                                "oracle_sha256",
                                "evaluator_sha256",
                                "runtime_sha256",
                                "image",
                            ]
                        },
                    }
                )
                transcript = canonical_bytes({"synthetic_attempt": attempt})
                audit = canonical_bytes({"not_learning_episode": attempt})
                signed = sign_outcome(
                    assignment=assignment,
                    issuer_id="trusted",
                    private_key=key,
                    outcome_bytes=outcome,
                    transcript_bytes=transcript,
                    episode_bytes=audit,
                )
                outcomes[attempt] = OutcomeEvidence(signed, outcome, transcript, audit)
                records[attempt] = {
                    "assignment_sha256": assignment_digest(assignment),
                    "status": "scored",
                    "physical_ids": ["send-" + attempt],
                    "evidence_sha256": digest(signed),
                    "snapshot_sha256": identity(attempt),
                    "budget_status": "verified_within_budget",
                    "cost_usd": 0.01,
                    "cleanup_status": "verified",
                }
    return cells, analysis, key, outcomes, records


def report(
    case, *, events=None, mutate_catalog=None, catalog_key=None, outcome_key=None, schedule_pin=None
):
    cells, analysis, key, outcomes, records = case
    payload = {
        "schema_version": "sibyl-sealed-terminal-catalog-v1",
        "issuer_id": "trusted",
        "schedule_sha256": schedule_digest(cells),
        "analysis_sha256": identity(asdict(analysis)),
        "cells": records,
    }
    if mutate_catalog:
        mutate_catalog(payload)
    return summarize_sealed(
        cells,
        analysis,
        sign_catalog(payload, catalog_key or key),
        outcomes,
        events or {},
        expected_schedule_sha256=schedule_pin or schedule_digest(cells),
        expected_analysis_sha256=identity(asdict(analysis)),
        trusted_catalog_key=key.public_key(),
        trusted_outcome_key=(outcome_key or key).public_key(),
        trusted_issuer_id="trusted",
    )


def test_sealed_report_complete_paired_primary_and_gate(sealed_report_case):
    value = report(sealed_report_case)
    assert value["scheduled_cells"] == CELL_COUNT
    assert value["primary_summary_contrast"] == {"estimate": 1, "interval_95": [1, 1]}
    assert value["statistical_release_gate"] is True
    assert value["release_ready"] is False
    assert value["inference"]["simultaneous_quantiles"] == [0.05 / 6, 1 - 0.05 / 6]
    assert len(value["category_cluster_rates"]["related_transfer"]) == RELATED_CLUSTERS
    assert all(
        row["interval_95"] == [0, 0]
        for key, row in value["exploratory"].items()
        if key.startswith("change:")
    )


@pytest.mark.parametrize(
    "mutation",
    ["cell_missing", "duplicate_cell", "pair_seed", "runtime", "membership", "source_policy"],
)
def test_sealed_report_schedule_denials(sealed_report_case, mutation):
    cells = sealed_report_case[0]
    if mutation == "cell_missing":
        cells.pop()
    elif mutation == "duplicate_cell":
        cells[-1] = cells[0]
    elif mutation == "pair_seed":
        old = cells[0]
        cells[0] = replace(
            old,
            assignment=old.assignment.model_copy(update={"seed": 4}),
            expected={**old.expected, "seed": 4},
        )
    elif mutation == "runtime":
        cells[0] = replace(
            cells[0], assignment=cells[0].assignment.model_copy(update={"runtime_sha256": "e" * 64})
        )
    elif mutation == "membership":
        cells[0] = replace(cells[0], cluster="foreign")
    else:
        cells[0] = replace(cells[0], bindings={**cells[0].bindings, "source_policy": "e" * 64})
    with pytest.raises(
        ValueError,
        match=r"schedule|identity|policy|membership|seed|physical|receipt|snapshot|catalog|assignment|submission",
    ):
        report(sealed_report_case)


@pytest.mark.parametrize(
    "mutation", ["missing", "physical_reuse", "receipt_reuse", "snapshot", "issuer", "assignment"]
)
def test_sealed_report_signed_catalog_denials(sealed_report_case, mutation):
    records = sealed_report_case[-1]
    ids = list(records)

    def mutate(payload):
        if mutation == "missing":
            payload["cells"].pop(ids[0])
        elif mutation == "physical_reuse":
            records[ids[1]]["physical_ids"] = records[ids[0]]["physical_ids"]
        elif mutation == "receipt_reuse":
            records[ids[1]]["evidence_sha256"] = records[ids[0]]["evidence_sha256"]
        elif mutation == "snapshot":
            records[ids[0]]["snapshot_sha256"] = "e" * 64
        elif mutation == "issuer":
            payload["issuer_id"] = "foreign"
        else:
            records[ids[0]]["assignment_sha256"] = "e" * 64

    with pytest.raises(
        ValueError,
        match=r"schedule|identity|policy|membership|seed|physical|receipt|snapshot|catalog|assignment|submission",
    ):
        report(sealed_report_case, mutate_catalog=mutate)


def test_sealed_report_unknowns_remain_in_denominator(sealed_report_case):
    cells, _, _, outcomes, records = sealed_report_case
    events = {}
    for cell in cells:
        if cell.assignment.arm_id != "sibyl_consolidation":
            continue
        i = cell.assignment.attempt_id
        outcomes.pop(i)
        events[i] = canonical_bytes({"actual_retained_unknown": i})
        records[i].update(
            status="unknown",
            evidence_sha256=digest(events[i]),
            snapshot_sha256=None,
            budget_status="unknown",
            cost_usd=None,
            cleanup_status="unknown",
        )
    value = report(sealed_report_case, events=events)
    assert value["scheduled_cells"] == CELL_COUNT
    assert value["terminal_statuses"]["unknown"] == SIBYL_CELL_COUNT
    assert value["unknown_cost_cells"] == SIBYL_CELL_COUNT
    assert value["primary_summary_contrast"]["estimate"] == 0
    assert value["statistical_release_gate"] is False


def test_sealed_report_signed_pass_with_unknown_budget_is_not_success(sealed_report_case):
    for row in sealed_report_case[-1].values():
        row["budget_status"] = "unknown"
    value = report(sealed_report_case)
    assert not any(row["success"] for row in value["cells"])
    assert value["statistical_release_gate"] is False


def test_sealed_report_primary_cannot_rescue_other_control(sealed_report_case):
    cells, _analysis, key, outcomes, records = sealed_report_case
    for cell in cells:
        if cell.assignment.arm_id != "no_memory":
            continue
        i = cell.assignment.attempt_id
        e = outcomes[i]

        raw = json.loads(e.outcome)
        raw["status"] = "passed"
        raw["passed"] = True
        data = canonical_bytes(raw)
        receipt = sign_outcome(
            assignment=cell.assignment,
            issuer_id="trusted",
            private_key=key,
            outcome_bytes=data,
            transcript_bytes=e.transcript,
            episode_bytes=e.audit,
        )
        outcomes[i] = replace(e, outcome=data, receipt=receipt)
        records[i]["evidence_sha256"] = digest(receipt)
    value = report(sealed_report_case)
    assert value["primary_summary_contrast"]["interval_95"][0] > 0
    assert value["release_contrasts"]["no_memory"]["bonferroni_three_95"] == [0, 0]
    assert value["statistical_release_gate"] is False


def test_sealed_report_joint_resampling_and_exact_quantiles():
    analysis = Analysis(7, 80)
    vectors = {"a": [0.0, 1.0, 2.0], "b": [0.0, 2.0, 4.0], "c": [1.0, 1.0, 1.0]}
    result = joint_intervals(vectors, analysis)
    assert result["contrasts"]["b"]["bonferroni_three_95"] == [
        2 * x for x in result["contrasts"]["a"]["bonferroni_three_95"]
    ]
    assert result["contrasts"]["c"]["unadjusted_95"] == [1, 1]
    assert result == joint_intervals(vectors, analysis)
    assert (
        result["contrasts"]["a"]["bonferroni_three_95"][0]
        <= result["contrasts"]["a"]["unadjusted_95"][0]
    )


@pytest.mark.parametrize("field", ["catalog_key", "outcome_key"])
def test_sealed_report_foreign_signature_denied(sealed_report_case, field):
    with pytest.raises(ValueError, match=r"invalid authenticated|invalid signed"):
        report(sealed_report_case, **{field: Ed25519PrivateKey.generate()})


def test_sealed_report_frozen_schedule_pin_denied(sealed_report_case):
    with pytest.raises(ValueError, match="preregistration"):
        report(sealed_report_case, schedule_pin="f" * 64)


def test_sealed_report_changed_signed_bytes_denied(sealed_report_case):
    outcomes = sealed_report_case[3]
    attempt = next(iter(outcomes))
    outcomes[attempt] = replace(outcomes[attempt], transcript=b"changed submission")
    with pytest.raises(ValueError, match="evidence differs"):
        report(sealed_report_case)


def test_sealed_report_curator_clusters_are_independent_units(sealed_report_case):
    cells = sealed_report_case[0]
    for index, cell in enumerate(cells):
        if cell.cluster == "cluster-1":
            cells[index] = replace(cell, cluster="cluster-0")
    value = report(sealed_report_case)
    assert value["release_contrasts"]["simple_summary"]["cluster_count"] == RELATED_CLUSTERS
    assert value["scheduled_cells"] == CELL_COUNT


@pytest.mark.parametrize(
    "failure", ["missing", "abstained_pack", "transport", "cleanup", "timeout", "budget", "grader"]
)
def test_sealed_report_signed_operational_failures_retained(sealed_report_case, failure):
    attempt = next(iter(sealed_report_case[3]))
    sealed_report_case[3].pop(attempt)
    event = canonical_bytes({"failure": failure, "attempt": attempt})
    sealed_report_case[-1][attempt].update(
        status=failure, evidence_sha256=digest(event), snapshot_sha256=None
    )
    value = report(sealed_report_case, events={attempt: event})
    assert value["terminal_statuses"][failure] == 1
    assert value["scheduled_cells"] == CELL_COUNT


def rebind_revision(case, index, revision):
    cells, _, key, outcomes, records = case
    cell = cells[index]
    assignment = cell.assignment.model_copy(update={"experiment_revision": revision})
    cells[index] = replace(cell, assignment=assignment)
    evidence = outcomes[assignment.attempt_id]
    signed = sign_outcome(
        assignment=assignment,
        issuer_id="trusted",
        private_key=key,
        outcome_bytes=evidence.outcome,
        transcript_bytes=evidence.transcript,
        episode_bytes=evidence.audit,
    )
    outcomes[assignment.attempt_id] = replace(evidence, receipt=signed)
    records[assignment.attempt_id].update(
        assignment_sha256=assignment_digest(assignment), evidence_sha256=digest(signed)
    )


def test_sealed_report_mixed_signed_checkpoint_revision_refused(sealed_report_case):
    rebind_revision(sealed_report_case, 0, "foreign-revision")
    with pytest.raises(ValueError, match="paired checkpoint experiment revision"):
        report(sealed_report_case)


def test_sealed_report_separately_frozen_checkpoint_revisions_allowed(sealed_report_case):
    for index, cell in enumerate(sealed_report_case[0]):
        rebind_revision(
            sealed_report_case, index, identity({"checkpoint_manifest": cell.assignment.checkpoint})
        )
    assert report(sealed_report_case)["scheduled_cells"] == CELL_COUNT
