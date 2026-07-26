"""Variant slicer: two boundary-rule fixes the v1 measurements argue for.

FIX 1 - band-aware packing. v1 flushes as soon as adding the next sibling would
cross TARGET_HI, which strands 29.5% of enterprise slices below the 600-char
floor. v2 only flushes once the buffer has cleared TARGET_LO, allowing growth up
to HARD_MAX while it is still under-full.

FIX 2 - bounded ancestor prepend. v1 prepends every descended ancestor line onto
the first child slice, which pushed 651 enterprise slices past the hard max (max
4,103 chars). v2 prepends at most MAX_PREPEND_CHARS worth of ancestor lines and
lets the breadcrumb carry the rest.
"""

from __future__ import annotations

from slicer import (
    HARD_MAX,
    TARGET_HI,
    TARGET_LO,
    Node,
    Slice,
    _flatten,
    breadcrumb_for,
    build_forest,
)

MAX_PREPEND_CHARS = 400
TAIL_MERGE = True


def _emit_v2(
    nodes: list[Node],
    ancestors: list[Node],
    lines: list[str],
    out: list[Slice],
    stats: dict[str, int],
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
            _emit_v2(node.children, [*ancestors, node], lines, child_slices, stats)
            if child_slices:
                first = child_slices[0]
                if len(node.line) + len(first.content) <= HARD_MAX:
                    first.line_indices = [node.index, *first.line_indices]
                    first.content = "\n".join(lines[i] for i in first.line_indices)
                else:
                    stats["prepend_deferred"] += 1
                    child_slices.insert(
                        0,
                        Slice(
                            [node.index],
                            node.line,
                            node.depth,
                            breadcrumb_for(ancestors),
                            "ancestor-line",
                        ),
                    )
                child_slices[0].breadcrumb = breadcrumb_for(ancestors)
                out.extend(child_slices)
            continue
        if size > HARD_MAX:
            flush()
            stats["oversize_leaf"] += 1
            out.append(
                Slice(
                    [node.index], node.line, node.depth, breadcrumb_for(ancestors), "oversize-leaf"
                )
            )
            continue
        # band-aware flush: only close a slice once it has cleared the floor
        if (buffer_chars >= TARGET_LO and buffer_chars + size > TARGET_HI) or (
            buffer_chars and buffer_chars + size > HARD_MAX
        ):
            flush()
        buffer.append(node)
        buffer_chars += size
    flush()
    # FIX 3 - tail merge: a stranded sub-floor tail is folded back into the
    # previous slice when the result still fits under the hard max. Each descend
    # opens a fresh sibling list, so v1 strands one short tail per descend.
    if TAIL_MERGE and len(out) >= 2:
        last, previous = out[-1], out[-2]
        if (
            len(last.content) < TARGET_LO
            and last.cut_depth == previous.cut_depth
            and len(previous.content) + len(last.content) + 1 <= HARD_MAX
        ):
            previous.line_indices = [*previous.line_indices, *last.line_indices]
            previous.content = "\n".join(lines[i] for i in previous.line_indices)
            out.pop()
            stats["tail_merges"] += 1


def slice_body_v2(body: str) -> tuple[list[Slice], dict[str, int]]:
    lines = body.split("\n")
    if not body.strip():
        return [], {
            "descend_events": 0,
            "oversize_leaf": 0,
            "prepend_deferred": 0,
            "tail_merges": 0,
        }
    roots = build_forest(lines)
    stats = {"descend_events": 0, "oversize_leaf": 0, "prepend_deferred": 0, "tail_merges": 0}
    out: list[Slice] = []
    _emit_v2(roots, [], lines, out, stats)
    return out, stats
