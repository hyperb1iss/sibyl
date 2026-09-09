"""Compose an authored task catalog into a local trusted-development manifest."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from benchmarks.agent_tasks import coding_controller
from benchmarks.agent_tasks.manifest import (
    Artifact,
    ControllerBudget,
    JsonOracleChecker,
    Manifest,
    ManifestError,
    Task,
    canonical_bytes,
    checker_artifacts,
    digest,
    identity,
    load_manifest,
    read_artifact,
    runtime_identity,
    strict_json,
    validate_partitions,
)


def _task_inputs(root: Path, tasks: list[Task]) -> dict[str, bytes]:
    inputs: dict[str, bytes] = {}
    for task in tasks:
        for artifact in [
            task.prompt,
            *checker_artifacts(task.checker),
            *(item.artifact for item in task.workspace),
        ]:
            content = read_artifact(root, artifact)
            if artifact.path in inputs and inputs[artifact.path] != content:
                raise ManifestError("conflicting catalog artifact paths")
            if artifact.path in {"manifest.json", "catalog.json", "composition.json"}:
                raise ManifestError("catalog uses reserved composition path")
            inputs[artifact.path] = content

    return inputs


def _catalog_document(catalog_bytes: bytes) -> dict[str, Any]:
    """Validate the catalog envelope before reading tasks or execution inputs."""
    catalog = strict_json(catalog_bytes)
    if not isinstance(catalog, dict):
        raise ManifestError("authored catalog must be an object")
    if catalog.get("schema_version") != "sibyl-authored-repair-catalog-v1":
        raise ManifestError("unsupported authored catalog")
    if catalog.get("experiences") != []:
        raise ManifestError("authored catalog cannot declare collected experiences")
    if not isinstance(catalog.get("tasks"), list):
        raise ManifestError("authored catalog tasks must be an array")
    seed = catalog.get("seed")
    if type(seed) is not int or seed < 0:
        raise ManifestError("authored catalog seed must be a nonnegative integer")
    return catalog


def compose(
    catalog_path: Path,
    output: Path,
    *,
    experiment_id: str,
    model: str,
    budget: ControllerBudget,
    dependency_lock: Path,
    allow_mechanism_transfer: bool = False,
) -> Path:
    """Bind current interpreter and installed controller without executing a task.

    Shared mechanism clusters are exploratory transfer only, never independent
    unseen-mechanism holdouts. An explicit acknowledgement is retained alongside
    the complete catalog and manifest. No experience or derived-memory arm is
    invented by this initial no-memory composition.
    """
    if catalog_path.is_symlink():
        raise ManifestError("catalog must not be a symlink")
    catalog_bytes = catalog_path.read_bytes()
    catalog = _catalog_document(catalog_bytes)
    tasks = [Task.model_validate(item) for item in catalog["tasks"]]
    if not tasks or any(task.split == "sealed" for task in tasks):
        raise ManifestError("authored catalog supports learning/development tasks only")
    clusters = catalog.get("mechanism_clusters", {})
    if (
        not isinstance(clusters, dict)
        or set(clusters) != {task.family_id for task in tasks}
        or any(not isinstance(value, str) or not value.strip() for value in clusters.values())
    ):
        raise ManifestError("every family needs an explicit mechanism cluster")
    cluster_splits: dict[str, set[str]] = {}
    for task in tasks:
        cluster_splits.setdefault(clusters[task.family_id], set()).add(task.split)
    shared = sorted(cluster for cluster, splits in cluster_splits.items() if len(splits) > 1)
    if shared and not allow_mechanism_transfer:
        raise ManifestError("shared mechanism clusters require explicit exploratory transfer")
    if any(not isinstance(task.checker, JsonOracleChecker) for task in tasks):
        raise ManifestError("authored tasks require the host-owned JSON oracle")
    checker = tasks[0].checker
    assert isinstance(checker, JsonOracleChecker)
    if any(
        (task.checker.image, task.checker.docker, task.checker.docker_host)
        != (checker.image, checker.docker, checker.docker_host)
        for task in tasks
        if isinstance(task.checker, JsonOracleChecker)
    ):
        raise ManifestError("one composition requires one pinned execution environment")
    inputs = _task_inputs(catalog_path.parent, tasks)

    def bind(name: str, data: bytes) -> Artifact:
        if name in inputs:
            raise ManifestError("catalog uses reserved execution artifact path")
        inputs[name] = data
        return Artifact(path=name, sha256=digest(data))

    controller = bind("execution/controller.py", Path(coding_controller.__file__).read_bytes())
    lock = bind("execution/uv.lock", dependency_lock.read_bytes())
    empty = bind("execution/empty-memory.txt", b"")
    args = [
        "--image",
        checker.image,
        "--tool-timeout",
        "45.0",
        "--memory-mb",
        "512",
        "--docker",
        checker.docker,
    ]
    if checker.docker_host:
        args.extend(["--docker-host", checker.docker_host])
    manifest = Manifest(
        schema_version="sibyl-agent-task-manifest-v1",
        experiment_id=experiment_id,
        purpose="trusted_development",
        runtime_sha256=identity(runtime_identity()),
        dependency_lock=lock,
        seed=catalog["seed"],
        controller={"script": controller, "args": args},
        controller_api_key_env="OPENROUTER_API_KEY",
        controller_model=model,
        controller_tools=coding_controller.REQUIRED_TOOLS,
        controller_budget=budget,
        controller_timeout_seconds=900.0,
        checker_timeout_seconds=300.0,
        experiences=[],
        tasks=tasks,
        arms=[{"id": "no-memory", "memory_pack": empty, "learning_source_ids": []}],
    )
    validate_partitions(manifest)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in inputs.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    path = output / "manifest.json"
    path.write_bytes(canonical_bytes(manifest.model_dump(mode="json")))
    load_manifest(path)
    (output / "catalog.json").write_bytes(catalog_bytes)
    (output / "composition.json").write_bytes(
        canonical_bytes(
            {
                "schema_version": "sibyl-authored-corpus-composition-v1",
                "catalog_sha256": digest(catalog_bytes),
                "manifest_sha256": digest(path.read_bytes()),
                "shared_mechanism_clusters": shared,
                "exploratory_mechanism_transfer": allow_mechanism_transfer,
                "independent_statistical_families": False,
                "sealed": False,
                "experience_count": 0,
            }
        )
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--dependency-lock", type=Path, required=True)
    parser.add_argument("--allow-mechanism-transfer", action="store_true")
    args = parser.parse_args()
    compose(
        args.catalog,
        args.output,
        experiment_id=args.experiment_id,
        model=args.model,
        budget=ControllerBudget.model_validate(strict_json(args.budget.read_bytes())),
        dependency_lock=args.dependency_lock,
        allow_mechanism_transfer=args.allow_mechanism_transfer,
    )


if __name__ == "__main__":
    main()
