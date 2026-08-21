"""Immutable local input bindings for LongMemEval-V2 release stages."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.bench import longmemeval_v2_rig as rig

OFFICIAL_DATASET_REVISION = "f152293e235517d504809563c833d7190b8c713b"
DOMAINS = ("web", "enterprise")
MEMORY_ARTIFACT_NAMES = (
    "memory_config.json",
    "chunk_catalog.jsonl.gz",
    "memory_manifest.json",
)
DATASET_ARTIFACT_NAMES = {
    "questions": "questions.jsonl",
    "trajectories": "trajectories.jsonl",
    "small_haystack": "haystacks/lme_v2_small.json",
}
MEMORY_ROOT_KEYS = frozenset({"baseline", "render"})
UPSTREAM_KEYS = frozenset({"aa_receipt", "paired_passes", "preregistration"})
HEX_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


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


def canonical_sha256(value: object) -> str:
    return rig.canonical_sha256(value)


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
    require_exact_keys(
        raw,
        frozenset({"path", "sha256", "size_bytes"}),
        name=name,
    )
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


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise StagePlanError("git is required to seal a local release stage")
    try:
        completed = subprocess.run(  # noqa: S603
            [git, *args],
            check=True,
            capture_output=True,
            cwd=root,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StagePlanError("could not inspect the Sibyl checkout") from exc
    return completed.stdout.strip()


def _repository_from_remote(remote: str) -> str:
    value = remote.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com":
            raise StagePlanError("origin must be a GitHub repository")
        value = parsed.path.lstrip("/")
    if len(value.split("/")) != 2 or any(not part for part in value.split("/")):
        raise StagePlanError("origin has no canonical owner/repository slug")
    return value


def discover_source_identity(root: Path) -> dict[str, str]:
    repository = _repository_from_remote(_git(root, "remote", "get-url", "origin"))
    ref = _git(root, "symbolic-ref", "-q", "HEAD")
    sha = _git(root, "rev-parse", "HEAD")
    if _git(root, "status", "--porcelain"):
        raise StagePlanError("release stage planning requires a clean checkout")
    if not ref.startswith("refs/heads/") or not HEX_SHA_PATTERN.fullmatch(sha):
        raise StagePlanError("release stage planning requires a named branch and exact SHA")
    remote_ref = ref.replace("refs/heads/", "refs/remotes/origin/", 1)
    if _git(root, "rev-parse", "--verify", remote_ref) != sha:
        raise StagePlanError("release stage SHA differs from its exact origin ref")
    return {"repository": repository, "ref": ref, "sha": sha}


def require_source_identity(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise StagePlanError("source identity is missing")
    require_exact_keys(
        raw,
        frozenset({"repository", "ref", "sha"}),
        name="source identity",
    )
    repository = require_string(raw.get("repository"), name="source identity.repository")
    ref = require_string(raw.get("ref"), name="source identity.ref")
    sha = require_string(raw.get("sha"), name="source identity.sha")
    if len(repository.split("/")) != 2:
        raise StagePlanError("source identity repository is not canonical")
    if not ref.startswith("refs/heads/") or not HEX_SHA_PATTERN.fullmatch(sha):
        raise StagePlanError("source identity ref or SHA is invalid")
    return {"repository": repository, "ref": ref, "sha": sha}


def dataset_record(data_root: Path) -> dict[str, Any]:
    root = data_root.expanduser().resolve()
    artifacts = {
        name: bind_artifact(root / relative, name=f"dataset {name}")
        for name, relative in DATASET_ARTIFACT_NAMES.items()
    }
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
    digests = {domain: canonical_sha256(sorted(ids[domain])) for domain in DOMAINS}
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


def _memory_root(raw: object, *, name: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != set(DOMAINS):
        raise StagePlanError(f"{name} memory root must cover both domains")
    domains: dict[str, Any] = {}
    for domain in DOMAINS:
        path = Path(require_string(raw.get(domain), name=f"{name}.{domain}"))
        path = path.resolve()
        domains[domain] = {
            "path": str(path),
            "artifacts": {
                filename: bind_artifact(
                    path / filename,
                    name=f"{name}.{domain}.{filename}",
                )
                for filename in MEMORY_ARTIFACT_NAMES
            },
        }
    return domains


def build_memory_bindings(spec: dict[str, Any]) -> dict[str, Any]:
    roots = spec["memory_roots"]
    baseline = _memory_root(roots["baseline"], name="baseline")
    render = _memory_root(roots["render"], name="render")
    if spec["stage"] == "aa" and spec["mode"] == "initial":
        if baseline is not None or render is not None:
            raise StagePlanError("initial A/A must create baseline memory inside the stage")
    elif baseline is None:
        raise StagePlanError("stage requires an externally completed baseline memory")
    if spec["stage"] == "render" and render is not None:
        raise StagePlanError("render stage must create fresh treatment memory")
    if spec["stage"] != "render" and render is not None:
        raise StagePlanError("non-render stage cannot bind treatment memory")
    return {"baseline": baseline, "render": render}


def require_memory_bindings(raw: object, *, spec: dict[str, Any]) -> None:
    if raw != build_memory_bindings(spec):
        raise StagePlanError("memory bindings changed after stage planning")


def _bind_aa_receipt(path: str) -> dict[str, Any]:
    artifact = bind_artifact(Path(path), name="upstream A/A receipt")
    receipt = rig.validate_aa_receipt(load_json(Path(artifact["path"])))
    return {
        **artifact,
        "status": receipt["status"],
        "aa_receipt_sha256": receipt["aa_receipt_sha256"],
        "passes": [
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "paired_pass_sha256": item["paired_pass_sha256"],
            }
            for item in receipt["passes"]
        ],
    }


def _bind_paired_pass(path: str) -> dict[str, Any]:
    artifact = bind_artifact(Path(path), name="upstream paired pass")
    paired = rig.validate_pass(load_json(Path(artifact["path"])))
    return {
        **artifact,
        "pass_id": paired["pass_id"],
        "seed": paired["seed"],
        "paired_pass_sha256": paired["paired_pass_sha256"],
    }


def _bind_preregistration(path: str, *, stage: str) -> dict[str, Any]:
    artifact = bind_artifact(Path(path), name="upstream preregistration")
    prereg = rig.validate_preregistration(load_json(Path(artifact["path"])), kind=stage)
    return {
        **artifact,
        "preregistration_sha256": prereg["preregistration_sha256"],
    }


def _require_upstream_authorization(spec: dict[str, Any], bindings: dict[str, Any]) -> None:
    stage = spec["stage"]
    mode = spec["mode"]
    aa = bindings["aa_receipt"]
    pairs = bindings["paired_passes"]
    prereg = bindings["preregistration"]
    if stage == "aa" and mode == "initial":
        if aa is not None or pairs or prereg is not None:
            raise StagePlanError("initial A/A cannot bind upstream score artifacts")
        return
    if stage == "aa":
        if aa is None or aa["status"] != "NEEDS_TWO_MORE":
            raise StagePlanError("A/A extension requires NEEDS_TWO_MORE")
        if len(pairs) != rig.INITIAL_AA_PASS_COUNT or prereg is not None:
            raise StagePlanError("A/A extension requires only the original three paired passes")
        bound_pairs = [
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "paired_pass_sha256": item["paired_pass_sha256"],
            }
            for item in pairs
        ]
        if bound_pairs != aa["passes"]:
            raise StagePlanError("A/A extension paired passes differ from its receipt")
        prior_ids = {item["pass_id"] for item in pairs}
        prior_seeds = {item["seed"] for item in pairs}
        if any(
            item["pass_id"] in prior_ids or item["seed"] in prior_seeds for item in spec["passes"]
        ):
            raise StagePlanError("A/A extension must use two fresh passes")
        return
    if stage == "anchor":
        if aa is None or aa["status"] != "PASS" or pairs or prereg is not None:
            raise StagePlanError("anchor requires only a passing A/A receipt")
        prior_seeds = {item["seed"] for item in aa["passes"]}
        if spec["passes"][0]["seed"] in prior_seeds:
            raise StagePlanError("anchor must use a fresh post-A/A seed")
        return
    if aa is not None or pairs or prereg is None:
        raise StagePlanError(f"{stage} requires only its sealed preregistration")


def build_upstream_bindings(spec: dict[str, Any]) -> dict[str, Any]:
    upstream = spec["upstream"]
    aa_path = upstream["aa_receipt"]
    prereg_path = upstream["preregistration"]
    bindings = {
        "aa_receipt": _bind_aa_receipt(aa_path) if aa_path is not None else None,
        "paired_passes": [_bind_paired_pass(path) for path in upstream["paired_passes"]],
        "preregistration": (
            _bind_preregistration(prereg_path, stage=spec["stage"])
            if prereg_path is not None
            else None
        ),
    }
    _require_upstream_authorization(spec, bindings)
    return bindings


def require_upstream_bindings(raw: object, *, spec: dict[str, Any]) -> None:
    if raw != build_upstream_bindings(spec):
        raise StagePlanError("upstream bindings changed after stage planning")
