"""Frozen inputs and declared split boundaries for trusted development tasks."""

from __future__ import annotations

import hashlib
import json
import platform
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "sibyl-agent-task-manifest-v1"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$"
FIXED_ENVIRONMENT = {"LANG": "C", "PYTHONHASHSEED": "0"}


class ManifestError(ValueError):
    """The experiment does not match its declared frozen inputs."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(value: Any) -> str:
    return digest(canonical_bytes(value))


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts)
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("expected a canonical relative file path")
    return value


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Artifact(FrozenModel):
    path: str
    sha256: str = Field(pattern=DIGEST_PATTERN)

    _path = field_validator("path")(relative_path)


class WorkspaceFile(FrozenModel):
    artifact: Artifact
    destination: str
    mode: Literal[420, 493] = 420  # Regular files and executable scripts only.

    _destination = field_validator("destination")(relative_path)


class Program(FrozenModel):
    script: Artifact
    args: list[str] = Field(default_factory=list)


class Experience(FrozenModel):
    id: str = Field(pattern=IDENTIFIER_PATTERN)
    family_id: str = Field(pattern=IDENTIFIER_PATTERN)
    split: Literal["learning", "development", "sealed"]
    revision: str = Field(min_length=1)
    artifact: Artifact


class Task(FrozenModel):
    id: str = Field(pattern=IDENTIFIER_PATTERN)
    family_id: str = Field(pattern=IDENTIFIER_PATTERN)
    split: Literal["development", "sealed"]
    prompt: Artifact
    workspace: list[WorkspaceFile]
    checker: Program


class Arm(FrozenModel):
    id: str = Field(pattern=IDENTIFIER_PATTERN)
    memory_pack: Artifact
    learning_source_ids: list[str]


class ControllerBudget(FrozenModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    cost_usd: float = Field(ge=0, allow_inf_nan=False)


class Manifest(FrozenModel):
    schema_version: Literal["sibyl-agent-task-manifest-v1"]
    experiment_id: str = Field(pattern=IDENTIFIER_PATTERN)
    purpose: Literal["trusted_development"]
    runtime_sha256: str = Field(pattern=DIGEST_PATTERN)
    dependency_lock: Artifact
    seed: int = Field(ge=0)
    controller: Program
    controller_model: str = Field(min_length=1)
    controller_tools: list[str]
    controller_budget: ControllerBudget
    controller_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    checker_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    experiences: list[Experience]
    tasks: list[Task] = Field(min_length=1)
    arms: list[Arm] = Field(min_length=1)


def runtime_identity() -> dict[str, Any]:
    """Bind the actual interpreter, OS and explicit subprocess environment."""
    return {
        "python": sys.version,
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "interpreter_sha256": digest(Path(sys.executable).resolve().read_bytes()),
        "environment": FIXED_ENVIRONMENT,
        "process_inspector_sha256": digest(Path("/bin/ps").read_bytes()),
    }


def read_artifact(root: Path, artifact: Artifact) -> bytes:
    path = root
    for part in PurePosixPath(artifact.path).parts:
        path /= part
        if path.is_symlink():
            raise ManifestError(f"symlink input: {artifact.path}")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ManifestError(f"non-file input: {artifact.path}")
    content = path.read_bytes()
    if digest(content) != artifact.sha256:
        raise ManifestError(f"changed input: {artifact.path}")
    return content


def _unique(values: list[str], label: str) -> None:
    if len(set(values)) != len(values):
        raise ManifestError(f"duplicate {label}")


def validate_partitions(manifest: Manifest) -> None:
    _unique([item.id for item in manifest.tasks], "task ID")
    _unique([item.id for item in manifest.arms], "arm ID")
    _unique([item.id for item in manifest.experiences], "experience ID")
    families: dict[str, str] = {}
    contents: dict[str, str] = {}
    for item in [*manifest.experiences, *manifest.tasks]:
        for key, index in (
            (item.family_id, families),
            ((item.artifact if isinstance(item, Experience) else item.prompt).sha256, contents),
        ):
            if key in index and index[key] != item.split:
                raise ManifestError("family or exact content overlaps experiment splits")
            index[key] = item.split
    experiences = {item.id: item for item in manifest.experiences}
    nonlearning_hashes = {
        item.artifact.sha256 for item in manifest.experiences if item.split != "learning"
    }
    for task in manifest.tasks:
        nonlearning_hashes.update([task.prompt.sha256, task.checker.script.sha256])
        nonlearning_hashes.update(item.artifact.sha256 for item in task.workspace)
    for arm in manifest.arms:
        if arm.memory_pack.sha256 != digest(b"") and arm.memory_pack.sha256 in nonlearning_hashes:
            raise ManifestError("memory pack overlaps a declared nonlearning artifact")
        _unique(arm.learning_source_ids, "pack source")
        if any(
            source_id not in experiences or experiences[source_id].split != "learning"
            for source_id in arm.learning_source_ids
        ):
            raise ManifestError("memory packs may reference only declared learning sources")
    for task in manifest.tasks:
        destinations = [item.destination for item in task.workspace]
        _unique(destinations, "workspace destination")
        if any(
            other.startswith(f"{path}/")
            for path in destinations
            for other in destinations
            if path != other
        ):
            raise ManifestError("workspace file/directory collision")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ManifestError(f"non-JSON numeric constant: {value}")


def strict_json(content: bytes) -> Any:
    """Reject ambiguous duplicate keys and JavaScript-only numeric constants."""
    return json.loads(content, object_pairs_hook=_strict_object, parse_constant=_reject_constant)


def load_manifest(path: Path) -> tuple[Manifest, dict[str, bytes]]:
    """Verify every binding before execution and retain the exact verified bytes."""
    if path.is_symlink():
        raise ManifestError("manifest must not be a symlink")
    manifest = Manifest.model_validate(strict_json(path.read_bytes()))
    if manifest.runtime_sha256 != identity(runtime_identity()):
        raise ManifestError("runtime identity differs from the frozen manifest")
    validate_partitions(manifest)
    artifacts = [manifest.dependency_lock, manifest.controller.script]
    artifacts.extend(item.artifact for item in manifest.experiences)
    artifacts.extend(arm.memory_pack for arm in manifest.arms)
    for task in manifest.tasks:
        artifacts.extend([task.prompt, task.checker.script])
        artifacts.extend(item.artifact for item in task.workspace)
    content: dict[str, bytes] = {}
    for artifact in artifacts:
        value = read_artifact(path.parent.resolve(), artifact)
        if artifact.path in content and content[artifact.path] != value:
            raise ManifestError("conflicting input bindings")
        content[artifact.path] = value
    for artifact in [
        *(task.prompt for task in manifest.tasks),
        *(arm.memory_pack for arm in manifest.arms),
    ]:
        try:
            content[artifact.path].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError(
                f"prompt and memory pack inputs require UTF-8: {artifact.path}"
            ) from exc
    for program in [manifest.controller, *(task.checker for task in manifest.tasks)]:
        if any("\x00" in arg for arg in program.args):
            raise ManifestError("program argument contains a null character")
    return manifest, content
