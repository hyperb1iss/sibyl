"""Execute one manifest-selected trusted development task."""

import argparse
import json
from pathlib import Path

from benchmarks.agent_tasks.runner import run_task


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = run_task(args.manifest, task_id=args.task, arm_id=args.arm, output=args.output)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(receipt, sort_keys=True))  # noqa: T201
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
