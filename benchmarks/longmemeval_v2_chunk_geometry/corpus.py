"""Shared corpus loading for the A1 measurement stages.

The official `lme_v2_small` haystack for every one of the 451 LongMemEval-V2
questions is exactly the 100-trajectory set captured in each domain's frozen
chunk catalog (verified: 211/211 enterprise and 240/240 web haystacks are
set-identical to the catalog). So the full question set can be scored against
these catalogs at official fidelity.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from paths import DATA_ROOT, OUT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from longmemeval_v2_diagnostics import (
    answer_evidence_phrases,
    is_exact_evidence_eligible,
    normalize_text,
)
from slicer import render_slice, slice_body, slice_header
from stage0 import load_states, state_evidence_text


@lru_cache(maxsize=2)
def domain_questions(domain: str) -> tuple[dict[str, Any], ...]:
    rows = [json.loads(line) for line in (DATA_ROOT / "questions.jsonl").read_text().splitlines()]
    return tuple(row for row in rows if row.get("domain") == domain)


def eligible_questions(domain: str) -> list[dict[str, Any]]:
    out = []
    for question in domain_questions(domain):
        phrases = answer_evidence_phrases(question)
        if phrases and all(is_exact_evidence_eligible(p) for p in phrases):
            out.append({"question": question, "phrases": phrases})
    return out


def build_units(domain: str) -> dict[str, Any]:
    """Return fat chunks, whole states, and A1 slices for one domain."""
    states = load_states(domain)
    fat: list[dict[str, Any]] = []
    slices: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    for key in sorted(states):
        entry = states[key]
        preamble = entry["preamble"]
        for part_index in sorted(entry["chunk_texts"]):
            fat.append(
                {
                    "key": key,
                    "part": part_index,
                    "index_text": preamble + entry["chunk_texts"][part_index],
                    "exposure_text": entry["chunk_texts"][part_index],
                }
            )
        state_rows.append(
            {
                "key": key,
                "index_text": preamble + state_evidence_text(entry),
                "exposure_text": state_evidence_text(entry),
            }
        )
        pieces, _ = slice_body(entry["tree"])
        total = len(pieces)
        for order, piece in enumerate(pieces, start=1):
            rendered = render_slice(
                slice_header(entry["state_index"], entry["url"], order, total),
                piece.breadcrumb,
                piece.content,
            )
            slices.append(
                {
                    "key": key,
                    "order": order,
                    "index_text": rendered,
                    "exposure_text": rendered,
                    "preamble": preamble,
                }
            )
    return {"states": states, "fat": fat, "state_rows": state_rows, "slices": slices}


__all__ = [
    "DATA_ROOT",
    "OUT",
    "build_units",
    "domain_questions",
    "eligible_questions",
    "normalize_text",
    "state_evidence_text",
]
