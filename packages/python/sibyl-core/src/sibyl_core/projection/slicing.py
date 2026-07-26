"""Cut an indented state body into reader-sized passages.

Ported from the A1 Stage 0/1 measurement harness, whose v2 boundary rules
produced every number the slice-substrate design rests on: zero straddle over
31,244 (question, phrase, carrier-state) triples, a 3-slice window reaching the
fat-state exposure ceiling, and a ~950-1,030 char mean slice.

Rules, in the order they fire:

  * cut on the shallowest indent depth whose subtrees land in a 600-1200 char
    band, packing siblings greedily
  * band-aware flush: a buffer under the floor keeps growing past TARGET_HI up
    to HARD_MAX rather than closing short
  * a subtree above HARD_MAX is descended one level and re-packed, its own line
    prepended to the first child slice unless the pair would cross HARD_MAX
  * a childless node above HARD_MAX is emitted whole, because cutting mid-line
    would bisect a literal
  * a stranded sub-floor tail is merged back into the slice before it when both
    were cut at the same depth
  * slices partition the body lines exactly: every line lands in exactly one
    slice, in order. The zero straddle rate is a consequence of that partition,
    not a property of the corpus.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import gcd
from urllib.parse import urlsplit

TARGET_LO = 600
TARGET_HI = 1200
HARD_MAX = 2000
HEADER_MAX_CHARS = 120

_URI_PATH_MAX_CHARS = 48
_NODE_NAME_MAX_CHARS = 40
_UNPARSED_LABEL_MAX_CHARS = 24

ACCESSIBILITY_NODE_PATTERN = re.compile(
    r"^(?:\[[^\]]+\]\s+)?(?P<role>[A-Za-z][\w-]*)\s+"
    r"(?P<quote>['\"])(?P<name>.*?)(?P=quote)(?P<attributes>,.*)?$"
)
_ROLE_ONLY_PATTERN = re.compile(r"^(?:\[[^\]]+\]\s+)?(?P<role>[A-Za-z][\w-]*)\b")


@dataclass
class Node:
    """A body line and the lines nested under it."""

    index: int
    depth: int
    line: str
    children: list[Node] = field(default_factory=list)
    subtree_chars: int = 0


@dataclass
class Slice:
    """A contiguous span of body lines and how it was cut."""

    line_indices: list[int]
    content: str
    cut_depth: int
    breadcrumb: str
    reason: str


@dataclass
class SliceStats:
    """Counters for the boundary rules that fired over one body."""

    descend_events: int = 0
    oversize_leaf: int = 0
    prepend_deferred: int = 0
    tail_merges: int = 0


def _line_cost(line: str) -> int:
    return len(line) + 1  # newline


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def detect_indent_unit(lines: Sequence[str]) -> tuple[str, int]:
    """Return the indent character and step width a body is written in.

    Callers hand us evidence they did not author. The measured corpus is
    tab-indented, and deriving depth from a tab count against a space-indented
    body would land every line at depth 0 and collapse the whole state into one
    slice, so the unit is detected rather than assumed.
    """
    widths: list[int] = []
    for line in lines:
        if not line.strip():
            continue
        indent = _leading_whitespace(line)
        if "\t" in indent:
            return "\t", 1
        widths.append(len(indent))
    step = 0
    for width in widths:
        step = gcd(step, width)
    return " ", step or 1


def line_depths(lines: Sequence[str]) -> list[int]:
    """Return the nesting depth of every line. Blank lines inherit the last."""
    character, step = detect_indent_unit(lines)
    depths: list[int] = []
    last_depth = 0
    for line in lines:
        if not line.strip():
            depths.append(last_depth)
            continue
        indent = _leading_whitespace(line)
        if character == "\t":
            last_depth = len(indent) - len(indent.lstrip("\t"))
        else:
            last_depth = len(indent) // step
        depths.append(last_depth)
    return depths


def build_forest(lines: Sequence[str]) -> list[Node]:
    """Build the indent forest over a body's lines."""
    roots: list[Node] = []
    stack: list[Node] = []
    for index, depth in enumerate(line_depths(lines)):
        node = Node(index=index, depth=depth, line=lines[index])
        while stack and stack[-1].depth >= depth:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    _measure(roots)
    return roots


def _measure(nodes: Sequence[Node]) -> None:
    for node in nodes:
        _measure(node.children)
        node.subtree_chars = _line_cost(node.line) + sum(
            child.subtree_chars for child in node.children
        )


def _flatten(node: Node) -> list[int]:
    indices = [node.index]
    for child in node.children:
        indices.extend(_flatten(child))
    return indices


def node_label(line: str) -> str:
    """Render one line as a breadcrumb segment."""
    stripped = line.strip()
    match = ACCESSIBILITY_NODE_PATTERN.fullmatch(stripped)
    if match is not None:
        name = " ".join(match.group("name").split())
        role = match.group("role")
        if name:
            return f"{role} '{name[:_NODE_NAME_MAX_CHARS]}'"
        return role
    role_match = _ROLE_ONLY_PATTERN.match(stripped)
    if role_match is not None:
        return role_match.group("role")
    return stripped[:_UNPARSED_LABEL_MAX_CHARS]


def breadcrumb_for(ancestors: Sequence[Node]) -> str:
    """Join every ancestor above a slice into its context trail.

    Uncapped on purpose: the measured configuration prepends the full chain, and
    dropping ancestors can only lower per-slice coverage. Capping is a later arm
    that has to earn its exposure number.
    """
    return " > ".join(node_label(node.line) for node in ancestors)


def _emit(
    nodes: Sequence[Node],
    ancestors: list[Node],
    lines: Sequence[str],
    out: list[Slice],
    stats: SliceStats,
) -> None:
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
                content="\n".join(lines[index] for index in indices),
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
            stats.descend_events += 1
            child_slices: list[Slice] = []
            _emit(node.children, [*ancestors, node], lines, child_slices, stats)
            if not child_slices:
                # Unreachable while every leaf emits, but the partition is the
                # invariant everything downstream rests on, so it is guaranteed
                # here structurally rather than argued.
                child_slices.append(Slice([node.index], node.line, node.depth, "", "ancestor-line"))
            elif len(node.line) + len(child_slices[0].content) <= HARD_MAX:
                first = child_slices[0]
                first.line_indices = [node.index, *first.line_indices]
                first.content = "\n".join(lines[index] for index in first.line_indices)
            else:
                stats.prepend_deferred += 1
                child_slices.insert(
                    0,
                    Slice([node.index], node.line, node.depth, "", "ancestor-line"),
                )
            child_slices[0].breadcrumb = breadcrumb_for(ancestors)
            out.extend(child_slices)
            continue
        if size > HARD_MAX:
            flush()
            stats.oversize_leaf += 1
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
        crosses_band = buffer_chars >= TARGET_LO and buffer_chars + size > TARGET_HI
        crosses_ceiling = buffer_chars > 0 and buffer_chars + size > HARD_MAX
        if crosses_band or crosses_ceiling:
            flush()
        buffer.append(node)
        buffer_chars += size
    flush()
    if len(out) >= 2:
        last, previous = out[-1], out[-2]
        if (
            len(last.content) < TARGET_LO
            and last.cut_depth == previous.cut_depth
            and len(previous.content) + len(last.content) + 1 <= HARD_MAX
        ):
            previous.line_indices = [*previous.line_indices, *last.line_indices]
            previous.content = "\n".join(lines[index] for index in previous.line_indices)
            out.pop()
            stats.tail_merges += 1


def slice_body(body: str) -> tuple[list[Slice], SliceStats]:
    """Cut a state body into slices that partition its lines exactly."""
    stats = SliceStats()
    if not body.strip():
        return [], stats
    lines = body.split("\n")
    out: list[Slice] = []
    _emit(build_forest(lines), [], lines, out, stats)
    return out, stats


def uri_path(uri: str) -> str:
    """Reduce a URI to the host label and path a header can afford."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return uri[:_URI_PATH_MAX_CHARS]
    path = parts.path or "/"
    if len(path) > _URI_PATH_MAX_CHARS:
        path = path[:22] + "…" + path[-24:]
    host = parts.netloc.split(".")[0] if parts.netloc else ""
    return f"{host}{path}" if host else path


def slice_header(ordinal: int, uri: str | None, index: int, total: int) -> str:
    """Render the locator line that opens a slice."""
    location = uri_path(uri) if uri else ""
    segments = [f"Observation {ordinal}"]
    if location:
        segments.append(location)
    segments.append(f"slice {index}/{total}")
    return " · ".join(segments)[:HEADER_MAX_CHARS]


def render_slice(header: str, breadcrumb: str, content: str) -> str:
    """Assemble the text a reader sees for one slice."""
    if breadcrumb:
        return f"{header}\n{breadcrumb}\n{content}"
    return f"{header}\n{content}"
