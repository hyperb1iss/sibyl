from __future__ import annotations

import subprocess
import sys


def test_memory_policy_import_does_not_eager_load_retrieval_search() -> None:
    script = (
        "from sibyl_core.auth.memory_policy import MemoryPolicyDecision; "
        "from sibyl_core.retrieval import context_search; "
        "assert MemoryPolicyDecision.__name__ == 'MemoryPolicyDecision'; "
        "assert callable(context_search)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
