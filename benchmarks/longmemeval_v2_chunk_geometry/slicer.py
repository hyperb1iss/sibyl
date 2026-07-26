"""Pure-function slicer for LongMemEval-V2 accessibility-tree state parts.

Rules implemented (A1 proposal):
  * cut on the shallowest indent depth whose subtrees land in a 600-1200 char band
  * a subtree above the hard max (2000) is descended one level and re-packed
  * a subtree that cannot be descended (single oversize line) is emitted whole,
    because cutting mid-line is forbidden
  * slices partition the body lines exactly: every line lands in exactly one slice
  * header <= 120 chars: "Observation {ordinal} · {uri_path} · slice {i}/{n}"
  * breadcrumb = role/name chain of ancestor nodes above the slice's first line
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

TARGET_LO = 600
TARGET_HI = 1200
HARD_MAX = 2000
HEADER_MAX_CHARS = 120

# Mirrors sibyl_core.projection.experience._ACCESSIBILITY_NODE_PATTERN.
_NODE_PATTERN = re.compile(
    r"^(?:\[[^\]]+\]\s+)?(?P<role>[A-Za-z][\w-]*)\s+"
    r"(?P<quote>['\"])(?P<name>.*?)(?P=quote)(?P<attributes>,.*)?$"
)
_ROLE_ONLY_PATTERN = re.compile(r"^(?:\[[^\]]+\]\s+)?(?P<role>[A-Za-z][\w-]*)\b")


@dataclass
class Node:
    index: int
    depth: int
    line: str
    children: list[Node] = field(default_factory=list)
    subtree_chars: int = 0
    subtree_lines: int = 0


@dataclass
class Slice:
    line_indices: list[int]
    content: str
    cut_depth: int
    breadcrumb: str
    reason: str  # "pack" | "descend-solo" | "oversize-leaf" | "line-split"


def _line_cost(line: str) -> int:
    return len(line) + 1  # newline


def build_forest(lines: list[str]) -> list[Node]:
    """Build the indent forest. Blank lines attach to the preceding node's depth."""
    roots: list[Node] = []
    stack: list[Node] = []
    last_depth = 0
    for index, line in enumerate(lines):
        stripped = line.lstrip("\t")
        if not line.strip():
            depth = last_depth
        else:
            depth = len(line) - len(stripped)
            last_depth = depth
        node = Node(index=index, depth=depth, line=line)
        while stack and stack[-1].depth >= depth:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    _measure(roots)
    return roots


def _measure(nodes: Iterable[Node]) -> None:
    for node in nodes:
        _measure(node.children)
        node.subtree_chars = _line_cost(node.line) + sum(c.subtree_chars for c in node.children)
        node.subtree_lines = 1 + sum(c.subtree_lines for c in node.children)


def node_label(line: str) -> str:
    stripped = line.strip()
    match = _NODE_PATTERN.fullmatch(stripped)
    if match is not None:
        name = " ".join(match.group("name").split())
        role = match.group("role")
        if name:
            return f"{role} '{name[:40]}'" if len(name) > 40 else f"{role} '{name}'"
        return role
    role_match = _ROLE_ONLY_PATTERN.match(stripped)
    if role_match is not None:
        return role_match.group("role")
    return stripped[:24]


def breadcrumb_for(ancestors: list[Node]) -> str:
    return " > ".join(node_label(node.line) for node in ancestors)


def _emit(
    nodes: list[Node],
    ancestors: list[Node],
    lines: list[str],
    out: list[Slice],
    stats: dict[str, int],
) -> None:
    """Greedy pack sibling subtrees at this level; descend when one is too big."""
    buffer: list[Node] = []
    buffer_chars = 0

    def flush() -> None:
        nonlocal buffer, buffer_chars
        if not buffer:
            return
        indices: list[int] = []
        for node in buffer:
            indices.extend(_flatten(node))
        out.append(
            Slice(
                line_indices=indices,
                content="\n".join(lines[i] for i in indices),
                cut_depth=buffer[0].depth,
                breadcrumb=breadcrumb_for(ancestors),
                reason="multi-subtree" if len(buffer) > 1 else "single-subtree",
            )
        )
        buffer = []
        buffer_chars = 0

    for node in nodes:
        size = node.subtree_chars
        if size > HARD_MAX and node.children:
            flush()
            stats["descend_events"] += 1
            child_slices: list[Slice] = []
            _emit(node.children, [*ancestors, node], lines, child_slices, stats)
            if child_slices:
                first = child_slices[0]
                first.line_indices = [node.index, *first.line_indices]
                first.content = "\n".join(lines[i] for i in first.line_indices)
                first.breadcrumb = breadcrumb_for(ancestors)
                out.extend(child_slices)
            else:  # pragma: no cover - a node with children always yields slices
                out.append(
                    Slice([node.index], node.line, node.depth, breadcrumb_for(ancestors), "pack")
                )
            continue
        if size > HARD_MAX:
            flush()
            stats["oversize_leaf"] += 1
            out.append(
                Slice(
                    [node.index],
                    node.line,
                    node.depth,
                    breadcrumb_for(ancestors),
                    "oversize-leaf",
                )
            )
            continue
        if buffer_chars and buffer_chars + size > TARGET_HI:
            flush()
        buffer.append(node)
        buffer_chars += size
    flush()


def _flatten(node: Node) -> list[int]:
    indices = [node.index]
    for child in node.children:
        indices.extend(_flatten(child))
    return indices


def slice_body(body: str) -> tuple[list[Slice], dict[str, int]]:
    lines = body.split("\n")
    if not body.strip():
        return [], {"descend_events": 0, "oversize_leaf": 0}
    roots = build_forest(lines)
    stats = {"descend_events": 0, "oversize_leaf": 0}
    out: list[Slice] = []
    _emit(roots, [], lines, out, stats)
    return out, stats


def uri_path(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url[:48]
    path = parts.path or "/"
    if len(path) > 48:
        path = path[:22] + "…" + path[-24:]
    host = parts.netloc.split(".")[0] if parts.netloc else ""
    return f"{host}{path}" if host else path


def slice_header(ordinal: int, url: str, index: int, total: int) -> str:
    header = f"Observation {ordinal} · {uri_path(url)} · slice {index}/{total}"
    return header[:HEADER_MAX_CHARS]


def render_slice(header: str, breadcrumb: str, content: str) -> str:
    if breadcrumb:
        return f"{header}\n{breadcrumb}\n{content}"
    return f"{header}\n{content}"
