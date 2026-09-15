"""Qualify the complete signed cohort through current source authorization."""

import base64
import hashlib
import json
import re
import tarfile
from collections import Counter
from uuid import NAMESPACE_URL, uuid5


def archive_material(path, expected_sha256):
    """Read only public provenance and evidence, never archived secrets."""
    selected = {}
    roots = {
        "configured-issuer.json",
        "inputs/manifest.json",
        "cohort-report.json",
        "stored-cohort.json",
    }
    digest = hashlib.sha256()
    with path.open("rb") as stream:

        class Reader:
            def read(self, size=-1):
                data = stream.read(size)
                digest.update(data)
                return data

        reader = Reader()
        with tarfile.open(fileobj=reader, mode="r|*") as archive:
            for member in archive:
                if member.name in roots or re.fullmatch(
                    r"attempts/[^/]+/(assignment.json|signed-(receipt|outcome|transcript|episode).bin)",
                    member.name,
                ):
                    if not member.isfile() or member.name in selected:
                        raise ValueError("ambiguous evidence archive member")
                    selected[member.name] = archive.extractfile(member).read()
        while reader.read(1024 * 1024):
            pass
    if digest.hexdigest() != expected_sha256:
        raise ValueError("consumed archive bytes changed")
    return selected


def verify_archive(material, source):
    """Verify every assigned slot; no outcome-based source selection."""
    # Deferred so importing this module never pulls in the signing stack.
    from benchmarks.agent_tasks.manifest import Manifest, identity  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: PLC0415

    from sibyl_core.tasks.eval_receipts import (  # noqa: PLC0415
        TaskAssignment,
        assignment_digest,
        verify_learning_evidence,
    )

    issuer = json.loads(material["configured-issuer.json"])
    manifest = Manifest.model_validate_json(material["inputs/manifest.json"])
    if (
        hashlib.sha256(material["inputs/manifest.json"]).hexdigest()
        != source["source_manifest_sha256"]
        or identity(manifest.model_dump(mode="json")) != source["experiment_revision"]
        or issuer["experiment_revision"] != source["experiment_revision"]
        or issuer["organization_id"] != source["organization_id"]
        or issuer["issuer_id"] != source["issuer_id"]
    ):
        raise ValueError("original issuer or manifest binding changed")
    key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(issuer["public_key_base64"], validate=True)
    )
    report = json.loads(material["cohort-report.json"])
    stored = json.loads(material["stored-cohort.json"])
    reported = {r["task_id"]: r for r in report["attempts"]}
    attempts = {r["attempt_id"]: r for r in stored["attempts"]}
    captures = {r["uuid"]: r for r in stored["captures"]}
    if (
        len(reported) != len(report["attempts"])
        or len(attempts) != len(stored["attempts"])
        or len(captures) != len(stored["captures"])
        or len(manifest.tasks) != len(attempts)
        or len(attempts) != source["registered_attempts"]
        or len(captures) != source["signed_admissions"]
    ):
        raise ValueError("complete cohort denominator differs")
    result, seen, admissions, counts = [], set(), set(), Counter()
    for task in manifest.tasks:
        assignment = TaskAssignment.model_validate_json(
            material[f"attempts/{task.id}/assignment.json"]
        )
        if (
            assignment.task_id != task.id
            or assignment.family_id != task.family_id
            or assignment.task_sha256 != identity(task.model_dump(mode="json"))
            or assignment.organization_id != source["organization_id"]
            or assignment.owner_principal_id != source["principal_id"]
            or assignment.experiment_id != issuer["experiment_id"]
            or assignment.experiment_revision != issuer["experiment_revision"]
            or assignment.controller_policy_sha256 != issuer["controller_policy_sha256"]
            or assignment.split != "learning"
            or assignment.arm_id != "no-memory"
            or assignment.checkpoint != 0
            or assignment.attempt_id in seen
        ):
            raise ValueError("registered assignment differs")
        seen.add(assignment.attempt_id)
        row, ledger = reported[task.id], attempts[assignment.attempt_id]
        item = {
            "assignment": assignment,
            "ledger": ledger,
            "capture": None,
            "status": row["runner_status"],
        }
        if not row["admitted"]:
            if row["runner_status"] != "controller_failed" or any(
                ledger.get(k) for k in ("capture_id", "receipt_sha256", "episode_sha256")
            ):
                raise ValueError("unadmitted slot changed")
        else:
            evidence = {
                kind: material[f"attempts/{task.id}/signed-{kind}.bin"]
                for kind in ("receipt", "outcome", "transcript", "episode")
            }
            proof = verify_learning_evidence(
                evidence["receipt"],
                trusted_public_key=key,
                trusted_issuer_id=issuer["issuer_id"],
                expected_assignment=assignment,
                expected_controller_policy_sha256=issuer["controller_policy_sha256"],
                outcome_bytes=evidence["outcome"],
                transcript_bytes=evidence["transcript"],
                episode_bytes=evidence["episode"],
            )
            capture_id = str(uuid5(NAMESPACE_URL, "sibyl-eval:" + proof.admission_id))
            capture = captures[capture_id]
            if (
                proof.admission_id in admissions
                or proof.outcome.status != row["runner_status"]
                or ledger["capture_id"] != capture_id
                or ledger["receipt_sha256"] != proof.receipt_sha256
                or ledger["episode_sha256"] != hashlib.sha256(evidence["episode"]).hexdigest()
                or capture["admission"]
                != {
                    "admission_id": proof.admission_id,
                    "assignment_sha256": assignment_digest(assignment),
                    "receipt_sha256": proof.receipt_sha256,
                }
            ):
                raise ValueError("original admission binding changed")
            admissions.add(proof.admission_id)
            item.update(
                capture=capture, receipt_base64=base64.b64encode(evidence["receipt"]).decode()
            )
        counts[item["status"]] += 1
        result.append(item)
    if (
        seen != set(attempts)
        or len(admissions) != source["signed_admissions"]
        or counts
        != {
            "passed": 190,
            "task_failed": 37,
            "candidate_failed": 5,
            "candidate_timeout": 1,
            "controller_failed": 7,
        }
    ):
        raise ValueError("historical outcome accounting differs")
    return issuer, result, dict(counts)


def verify_live_ledger(rows, verified):
    indexed = {r["attempt_id"]: r for r in rows}
    if len(indexed) != len(rows) or set(indexed) != {v["assignment"].attempt_id for v in verified}:
        raise ValueError("live attempt denominator differs")
    for item in verified:
        live = indexed[item["assignment"].attempt_id]
        if any(
            live.get(k) != item["ledger"].get(k)
            for k in ("capture_id", "receipt_sha256", "episode_sha256")
        ):
            raise ValueError("restored admission ledger differs")
        if item["capture"] and live.get("receipt_base64") != item["receipt_base64"]:
            raise ValueError("restored signed receipt bytes differ")
