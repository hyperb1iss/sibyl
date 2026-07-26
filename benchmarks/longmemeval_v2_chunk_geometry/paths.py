"""Filesystem roots for the chunk-geometry stages.

Every root is machine-local and overridable, because the two corpora these
stages read are build artifacts that are never committed: the frozen era-3
chunk catalogs and the downloaded LongMemEval-V2 question set. A git worktree
does not carry the primary checkout's `.moon/cache`, so the environment
overrides are the supported way to point a worktree at an existing corpus.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCHMARKS_DIR = HERE.parent
REPO_ROOT = BENCHMARKS_DIR.parent

DEFAULT_EVAL_ROOT = REPO_ROOT / ".moon/cache/evals/lme-v2-two-typed-29526606329"
DEFAULT_DATA_ROOT = REPO_ROOT / ".moon/cache/benchmarks/longmemeval-v2-full"


def _root(variable: str, default: Path) -> Path:
    override = os.environ.get(variable)
    return Path(override).expanduser() if override else default


# Frozen two-typed chunk catalogs: <EVAL_ROOT>/<domain>/memory_state/chunk_catalog.jsonl.gz
EVAL_ROOT = _root("SIBYL_A1_EVAL_ROOT", DEFAULT_EVAL_ROOT)

# LongMemEval-V2 full download, read for questions.jsonl.
DATA_ROOT = _root("SIBYL_A1_DATA_ROOT", DEFAULT_DATA_ROOT)

# Report destination. Defaults into the repo so a re-run diffs the committed receipts.
OUT = _root("SIBYL_A1_OUT", HERE / "out")

__all__ = ["BENCHMARKS_DIR", "DATA_ROOT", "EVAL_ROOT", "OUT", "REPO_ROOT"]
