"""Freeze authored repair tasks for trusted development, not model experiences."""

from __future__ import annotations

import argparse
import io
import json
import keyword
import tokenize
from difflib import SequenceMatcher
from pathlib import Path

from benchmarks.agent_tasks import coding_controller, json_oracle
from benchmarks.agent_tasks.corpus_families import Family, families
from benchmarks.agent_tasks.manifest import (
    Artifact,
    JsonOracleChecker,
    Task,
    WorkspaceFile,
    canonical_bytes,
    digest,
)


def normalized(text: str) -> list[str]:
    """Erase identifiers and literals to expose structural fixture similarity."""
    result = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type in {tokenize.COMMENT, tokenize.ENCODING, tokenize.NL, tokenize.ENDMARKER}:
            continue
        if token.type == tokenize.NAME and not keyword.iskeyword(token.string):
            result.append("NAME")
        elif token.type in {tokenize.NUMBER, tokenize.STRING}:
            result.append("LITERAL")
        else:
            result.append(token.string)
    return result


def lineage_audit(items: list[Family]) -> dict:
    """Report cross-split overlap; similarity is evidence for human review."""
    if any(not item.mechanism_cluster.strip() for item in items):
        raise ValueError("missing mechanism cluster")
    if len({item.id for item in items}) != len(items):
        raise ValueError("duplicate family identity")
    pairs = []
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if left.split == right.split:
                continue
            comparisons = []
            for lp, ls in left.workspace.items():
                for rp, rs in right.workspace.items():
                    comparisons.append(
                        {
                            "left_path": lp,
                            "right_path": rp,
                            "exact": ls == rs,
                            "normalized_similarity": SequenceMatcher(
                                None, normalized(ls), normalized(rs), autojunk=False
                            ).ratio(),
                        }
                    )
            pairs.append(
                {
                    "left": left.id,
                    "right": right.id,
                    "shared_lineage": left.lineage == right.lineage,
                    "shared_mechanism": left.mechanism_cluster == right.mechanism_cluster,
                    "exact_contract": left.contract == right.contract,
                    "overlapping_case_digests": sorted(
                        {
                            digest(
                                canonical_bytes(
                                    {"input": case["input"], "expected": case["expected"]}
                                )
                            )
                            for case in left.public_cases + left.private_cases
                        }
                        & {
                            digest(
                                canonical_bytes(
                                    {"input": case["input"], "expected": case["expected"]}
                                )
                            )
                            for case in right.public_cases + right.private_cases
                        }
                    ),
                    "files": comparisons,
                }
            )
    return {
        "schema_version": "sibyl-authored-corpus-lineage-v1",
        "pairs": pairs,
        "near_duplicate_clearance": "requires independent semantic review",
        "family_count": len(items),
        "mechanism_cluster_count": len({item.mechanism_cluster for item in items}),
        "independent_statistical_families": False,
        "experience_count": 0,
    }


def freeze(
    output: Path, *, seed: int, image: str, docker: str, docker_host: str | None = None
) -> Path:
    """Write runner Task records and bound private oracle inputs into a new root.

    This task catalog must be composed with a runtime-specific Manifest by the
    caller. Reference and partial repairs are intentionally never written here.
    """
    if seed < 0:
        raise ValueError("seed must be nonnegative")
    items = families(seed)
    # Validate runtime configuration before creating any output.
    placeholder = Artifact(path="unused", sha256="0" * 64)
    checker = JsonOracleChecker(
        schema_version="sibyl-json-cli-oracle-v1",
        oracle=placeholder,
        runtime=placeholder,
        evaluator=placeholder,
        argv=["python", "-B", "app.py"],
        image=image,
        docker=docker,
        docker_host=docker_host,
        timeout_seconds=10.0,
        memory_mb=256,
    )
    output.mkdir(mode=0o700, parents=False, exist_ok=False)

    def write(name: str, data: bytes) -> Artifact:
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return Artifact(path=name, sha256=digest(data))

    runtime = write("private/coding_controller.py", Path(coding_controller.__file__).read_bytes())
    evaluator = write("private/json_oracle.py", Path(json_oracle.__file__).read_bytes())
    tasks = []
    for family in items:
        task_id = f"{family.id}-{seed}"
        prefix = f"tasks/{task_id}"
        prompt = write(f"{prefix}/prompt.md", family.contract.encode())
        public = (
            "import json, subprocess, sys\n"
            f"cases = json.loads({json.dumps(family.public_cases)!r})\n"
            "for case in cases:\n"
            "    run = subprocess.run([sys.executable, '-B', 'app.py'], input=json.dumps(case['input']), text=True, capture_output=True, timeout=5, check=True)\n"
            "    assert json.loads(run.stdout) == case['expected'], case['id']\n"
        )
        workspace = [
            WorkspaceFile(
                artifact=write(f"{prefix}/workspace/{name}", data.encode()), destination=name
            )
            for name, data in (family.workspace | {"public_checks.py": public}).items()
        ]
        oracle = write(
            f"private/{task_id}.json",
            canonical_bytes(
                {
                    "schema_version": "sibyl-json-cli-cases-v1",
                    "cases": family.public_cases + family.private_cases,
                }
            ),
        )
        bound = checker.model_copy(
            update={"oracle": oracle, "runtime": runtime, "evaluator": evaluator}
        )
        tasks.append(
            Task(
                id=task_id,
                family_id=family.id,
                split=family.split,
                prompt=prompt,
                workspace=workspace,
                checker=bound,
            )
        )
    catalog = {
        "schema_version": "sibyl-authored-repair-catalog-v1",
        "seed": seed,
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "experiences": [],
        "lineages": {item.id: item.lineage for item in items},
        "mechanism_clusters": {item.id: item.mechanism_cluster for item in items},
        "limitations": [
            "authored fixtures, not collected episodes",
            "trusted development only",
            "six families do not satisfy the twenty-family release gate",
        ],
    }
    write("lineage-audit.json", canonical_bytes(lineage_audit(items)))
    write("tasks.json", canonical_bytes(catalog))
    return output / "tasks.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--image", required=True)
    parser.add_argument("--docker", default="/usr/bin/docker")
    parser.add_argument("--docker-host")
    arguments = parser.parse_args()
    freeze(
        arguments.output,
        seed=arguments.seed,
        image=arguments.image,
        docker=arguments.docker,
        docker_host=arguments.docker_host,
    )


if __name__ == "__main__":
    main()
