"""Immutable local input bindings for LongMemEval-V2 release stages."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from benchmarks.local_execution_identity import (
    GIT_SHA_LENGTH,
    is_canonical_repository,
    is_valid_branch_ref,
)
from tools.bench import longmemeval_v2_rig as rig

from sibyl_core.evals.longmemeval_v2 import (
    load_longmemeval_v2_haystack,
    load_longmemeval_v2_questions,
)

OFFICIAL_DATASET_REVISION = "f152293e235517d504809563c833d7190b8c713b"
OFFICIAL_DATASET_SHA256 = {
    "questions": ("sha256:0a3ae5ebea938c24d7800e1e0b0828e08ae1646f939a53853b2b8cdc08e292b7"),
    "trajectories": ("sha256:363cec9a8e87aa8d9101ce4e600aadbf7031d674056ebe4f969e8424abc5f3c6"),
    "small_haystack": ("sha256:9b5301defb23a088a5f06e45ff8d5f35e569d78305a66d492046a9fff9b46593"),
}
DOMAINS = ("web", "enterprise")
DATASET_ARTIFACT_NAMES = {
    "questions": "questions.jsonl",
    "trajectories": "trajectories.jsonl",
    "small_haystack": "haystacks/lme_v2_small.json",
}
MEMORY_ROOT_KEYS = frozenset({"baseline", "render"})
UPSTREAM_KEYS = frozenset({"aa_authorization", "preregistration_authorization"})


class StagePlanError(ValueError):
    """Raised when a local release stage cannot be sealed honestly."""


def require_exact_keys(raw: dict[str, Any], expected: frozenset[str], *, name: str) -> None:
    if set(raw) != expected:
        missing = sorted(expected - set(raw))
        unknown = sorted(set(raw) - expected)
        raise StagePlanError(f"{name} fields differ: missing={missing}, unknown={unknown}")


def require_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise StagePlanError(f"{name} must be a canonical non-empty string")
    return value


def require_positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StagePlanError(f"{name} must be a positive integer")
    return value


def require_nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StagePlanError(f"{name} must be a non-negative integer")
    return value


def require_positive_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StagePlanError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise StagePlanError(f"{name} must be finite and positive")
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def bind_artifact(path: Path, *, name: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise StagePlanError(f"{name} is missing: {resolved}")
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def require_artifact(raw: object, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError(f"{name} binding is missing")
    require_exact_keys(raw, frozenset({"path", "sha256", "size_bytes"}), name=name)
    path = Path(require_string(raw.get("path"), name=f"{name}.path")).resolve()
    expected_size = require_nonnegative_int(raw.get("size_bytes"), name=f"{name}.size_bytes")
    if not path.is_file() or path.stat().st_size != expected_size:
        raise StagePlanError(f"{name} path or size changed")
    if raw.get("sha256") != sha256_file(path):
        raise StagePlanError(f"{name} digest changed")
    return dict(raw)


def load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StagePlanError(f"could not load JSON object: {path}") from exc
    if not isinstance(raw, dict):
        raise StagePlanError(f"expected a JSON object: {path}")
    return raw


def require_source_identity(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise StagePlanError("source identity is missing")
    require_exact_keys(raw, frozenset({"repository", "ref", "sha"}), name="source identity")
    repository = require_string(raw.get("repository"), name="source identity.repository")
    ref = require_string(raw.get("ref"), name="source identity.ref")
    sha = require_string(raw.get("sha"), name="source identity.sha")
    if not is_canonical_repository(repository):
        raise StagePlanError("source identity repository is not canonical")
    if not is_valid_branch_ref(ref):
        raise StagePlanError("source identity ref is invalid")
    if len(sha) != GIT_SHA_LENGTH or any(character not in "0123456789abcdef" for character in sha):
        raise StagePlanError("source identity ref or SHA is invalid")
    return {"repository": repository, "ref": ref, "sha": sha}


def _canonical_root(path: Path, *, name: str) -> Path:
    expanded = path.expanduser()
    resolved = expanded.resolve()
    if not expanded.is_absolute() or expanded != resolved or not resolved.is_dir():
        raise StagePlanError(f"{name} must be one canonical non-symlinked directory")
    return resolved


def _contained_artifact(root: Path, relative: str, *, name: str) -> dict[str, Any]:
    candidate = root / relative
    resolved = candidate.resolve()
    if candidate != resolved or not resolved.is_relative_to(root):
        raise StagePlanError(f"{name} escapes its canonical root through a symlink")
    return bind_artifact(resolved, name=name)


def dataset_record(data_root: Path) -> dict[str, Any]:
    root = _canonical_root(data_root, name="dataset root")
    artifacts = {
        name: _contained_artifact(root, relative, name=f"dataset {name}")
        for name, relative in DATASET_ARTIFACT_NAMES.items()
    }
    if {name: artifact["sha256"] for name, artifact in artifacts.items()} != (
        OFFICIAL_DATASET_SHA256
    ):
        raise StagePlanError("dataset payload hashes differ from the pinned revision")
    ids = {domain: [] for domain in DOMAINS}
    questions_path = root / DATASET_ARTIFACT_NAMES["questions"]
    try:
        with questions_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise StagePlanError(f"questions.jsonl:{line_number} is not an object")
                domain = row.get("domain")
                question_id = row.get("id")
                if domain in ids and isinstance(question_id, str) and question_id:
                    ids[domain].append(question_id)
    except (OSError, json.JSONDecodeError) as exc:
        raise StagePlanError("dataset questions.jsonl is unreadable") from exc
    counts = {domain: len(ids[domain]) for domain in DOMAINS}
    digests = {domain: rig.canonical_sha256(sorted(ids[domain])) for domain in DOMAINS}
    official_counts = {domain: rig.OFFICIAL_SMALL_QUESTION_COUNTS[domain] for domain in DOMAINS}
    official_digests = {
        domain: rig.OFFICIAL_SMALL_QUESTION_IDS_SHA256[domain] for domain in DOMAINS
    }
    if counts != official_counts:
        raise StagePlanError("dataset does not contain the complete official Small corpus")
    if digests != official_digests:
        raise StagePlanError("dataset question IDs differ from the pinned Small corpus")
    return {
        "root": str(root),
        "revision": OFFICIAL_DATASET_REVISION,
        "question_count_by_domain": counts,
        "question_ids_sha256_by_domain": digests,
        "artifacts": artifacts,
    }


def require_dataset(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError("dataset binding is missing")
    require_exact_keys(
        raw,
        frozenset(
            {
                "root",
                "revision",
                "question_count_by_domain",
                "question_ids_sha256_by_domain",
                "artifacts",
            }
        ),
        name="dataset",
    )
    if raw.get("revision") != OFFICIAL_DATASET_REVISION:
        raise StagePlanError("dataset revision is not the sealed immutable snapshot")
    current = dataset_record(Path(require_string(raw.get("root"), name="dataset.root")))
    if current != raw:
        raise StagePlanError("dataset binding changed after stage planning")
    return dict(raw)


def _json_artifact_sha256(payload: object) -> str:
    encoded = (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _runtime_dataset_identity(dataset: dict[str, Any], *, domain: str) -> str:
    root = Path(dataset["root"])
    questions = [
        question
        for question in load_longmemeval_v2_questions(
            Path(dataset["artifacts"]["questions"]["path"])
        )
        if question.domain == domain
    ]
    runtime_questions: list[dict[str, Any]] = []
    for question in questions:
        row: dict[str, Any] = {
            "id": question.id,
            "domain": question.domain,
            "environment": question.environment,
            "question_type": question.question_type,
            "question": question.question,
            "answer": question.answer,
            "eval_function": question.eval_function,
        }
        if question.image is not None:
            image_path = root / question.image
            if not image_path.is_file():
                raise StagePlanError(f"dataset question image is missing: {question.image}")
            row["question"] = {"text": question.question, "image": str(image_path.resolve())}
        runtime_questions.append(row)
    haystack = load_longmemeval_v2_haystack(Path(dataset["artifacts"]["small_haystack"]["path"]))
    runtime_haystack = {question.id: list(haystack[question.id]) for question in questions}
    dataset_hashes = {
        "questions_sha256": dataset["artifacts"]["questions"]["sha256"],
        "trajectories_sha256": dataset["artifacts"]["trajectories"]["sha256"],
        "haystack_sha256": dataset["artifacts"]["small_haystack"]["sha256"],
    }
    return rig.canonical_sha256(
        {
            **dataset_hashes,
            "runtime_questions_sha256": _json_artifact_sha256(runtime_questions),
            "runtime_haystack_sha256": _json_artifact_sha256(runtime_haystack),
            "selected_question_ids_sha256": dataset["question_ids_sha256_by_domain"][domain],
        }
    )


def build_expected_stack(
    *,
    source: dict[str, str],
    official_source: dict[str, Any],
    dataset: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Derive the exact bridge stack identity before any provider work."""
    stack = {
        "sibyl_commit": source["sha"],
        "sibyl_git_status": "clean",
        "official_source": official_source,
        "dataset_sha256_by_domain": {
            domain: _runtime_dataset_identity(dataset, domain=domain) for domain in DOMAINS
        },
        "reader": {
            "model": runtime["reader_model"],
            "base_url": runtime["reader_base_url"],
        },
        "judge": {"model": runtime["evaluator_model"]},
    }
    return rig.validate_stack(stack)
