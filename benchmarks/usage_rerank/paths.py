"""Filesystem and connection settings for the usage-rerank feasibility harness.

Every setting is machine-local and overridable, because the store this harness
reads is a live SurrealDB content database rather than a committed fixture. The
defaults point at the local dev stack so a bare `python extract.py` reproduces
the committed receipts against a developer's own dogfooding data.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCHMARKS_DIR = HERE.parent
REPO_ROOT = BENCHMARKS_DIR.parent

DEFAULT_SURREAL_HTTP_URL = "http://127.0.0.1:8000"
DEFAULT_CONTENT_NAMESPACE = "sibyl_content"
DEFAULT_CONTENT_DATABASE = "content"


def _env(variable: str, default: str) -> str:
    value = os.environ.get(variable)
    return value if value else default


# HTTP base of the SurrealDB instance holding the content database. The harness
# only ever issues SELECT/RETURN, so pointing this at a shared dev store is safe.
SURREAL_HTTP_URL = _env("SIBYL_P5_SURREAL_HTTP_URL", DEFAULT_SURREAL_HTTP_URL)
SURREAL_USERNAME = _env("SIBYL_P5_SURREAL_USERNAME", "root")
SURREAL_PASSWORD = _env("SIBYL_P5_SURREAL_PASSWORD", "root")

# memory_usage_events lives in the shared content namespace, not an org namespace.
# Mirrors SurrealContentClient's defaults in
# packages/python/sibyl-core/src/sibyl_core/backends/surreal/content_client.py.
CONTENT_NAMESPACE = _env("SIBYL_P5_CONTENT_NAMESPACE", DEFAULT_CONTENT_NAMESPACE)
CONTENT_DATABASE = _env("SIBYL_P5_CONTENT_DATABASE", DEFAULT_CONTENT_DATABASE)

# Report destination. Defaults into the repo so a re-run diffs the committed receipts.
OUT = Path(_env("SIBYL_P5_OUT", str(HERE / "out"))).expanduser()

EVENTS_JSONL = OUT / "usage_events.jsonl"
SESSIONS_JSONL = OUT / "exposure_sessions.jsonl"
EXTRACT_SUMMARY_JSON = OUT / "extract_summary.json"
WHATIF_REPORT_JSON = OUT / "whatif_report.json"

__all__ = [
    "BENCHMARKS_DIR",
    "CONTENT_DATABASE",
    "CONTENT_NAMESPACE",
    "EVENTS_JSONL",
    "EXTRACT_SUMMARY_JSON",
    "OUT",
    "REPO_ROOT",
    "SESSIONS_JSONL",
    "SURREAL_HTTP_URL",
    "SURREAL_PASSWORD",
    "SURREAL_USERNAME",
    "WHATIF_REPORT_JSON",
]
