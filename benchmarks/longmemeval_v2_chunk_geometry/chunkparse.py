"""Parse LME-v2 chunk-catalog records into preamble / state-header / body segments.

Observed chunk layout:

    Trajectory: <id>
    Domain / Environment / Outcome / Goal: <multi-line> / Start URL: ...
    <blank>
    State <n>
    URL: ...
    [Part i/n]
    [blank]
    [Action: ...]   (order of Action/Thought varies)
    [Thought: ...]
    Accessibility tree:        <- present only on part 0
    <tree lines>

Continuation parts (part index > 0) carry the trajectory preamble + State/URL/Part
header and then resume the raw tree text.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from paths import EVAL_ROOT

_STATE_LINE = re.compile(r"^State (\d+)$", re.MULTILINE)
_PART_LINE = re.compile(r"^Part (\d+)/(\d+)$")
_TREE_MARKER = "Accessibility tree:"


def catalog_path(domain: str) -> Path:
    return EVAL_ROOT / domain / "memory_state" / "chunk_catalog.jsonl.gz"


def iter_chunks(domain: str) -> Iterator[dict[str, Any]]:
    with gzip.open(catalog_path(domain), "rt") as handle:
        for line in handle:
            yield json.loads(line)


def split_chunk(content: str) -> dict[str, Any]:
    match = _STATE_LINE.search(content)
    if match is None:
        return {
            "preamble": content,
            "state_index": None,
            "url": "",
            "part": None,
            "header": "",
            "body": "",
        }
    preamble = content[: match.start()]
    lines = content[match.start() :].split("\n")
    state_index = int(match.group(1))
    url = ""
    part = None
    cursor = 1
    header_lines = [lines[0]]
    while cursor < len(lines):
        line = lines[cursor]
        if line.startswith("URL: "):
            url = line[5:]
        elif (nums := _PART_LINE.fullmatch(line)) is not None:
            part = (int(nums.group(1)), int(nums.group(2)))
        else:
            break
        header_lines.append(line)
        cursor += 1
    tree_at = None
    for index in range(cursor, len(lines)):
        if lines[index] == _TREE_MARKER:
            tree_at = index
            break
    if tree_at is not None:
        header_lines.extend(lines[cursor : tree_at + 1])
        body_start = tree_at + 1
    else:
        body_start = cursor
        while body_start < len(lines) and not lines[body_start].strip():
            body_start += 1
    return {
        "preamble": preamble,
        "state_index": state_index,
        "url": url,
        "part": part,
        "header": "\n".join(header_lines),
        "body": "\n".join(lines[body_start:]),
    }
