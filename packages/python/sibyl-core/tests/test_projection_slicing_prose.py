from __future__ import annotations

from sibyl_core.projection.slicing import (
    HARD_MAX,
    TARGET_LO,
    prose_line_depths,
    slice_prose,
)


def _paragraph(word: str, count: int = 40) -> str:
    return " ".join([word] * count)


def _document() -> str:
    """A document deep and wide enough to force packing and a descend."""
    lines: list[str] = ["# Runbook", "", _paragraph("intro"), ""]
    for section in range(4):
        lines.extend([f"## Section {section}", ""])
        for topic in range(3):
            lines.extend(
                [
                    f"### Topic {section}.{topic}",
                    "",
                    _paragraph(f"body{section}{topic}"),
                    "",
                    f"- first item for {section}.{topic}",
                    f"- second item for {section}.{topic}",
                    "",
                ]
            )
    return "\n".join(lines)


def _partition(body: str) -> list[int]:
    slices, _ = slice_prose(body)
    indices: list[int] = []
    for entry in slices:
        indices.extend(entry.line_indices)
    return indices


def test_prose_slices_partition_the_body_lines_exactly() -> None:
    body = _document()

    assert _partition(body) == list(range(len(body.split("\n"))))


def test_an_empty_prose_body_yields_no_slices() -> None:
    slices, stats = slice_prose("   \n\n  ")

    assert slices == []
    assert stats.descend_events == 0


def test_prose_nests_by_heading_level_not_indentation() -> None:
    lines = [
        "# Title",
        "top matter",
        "## Section",
        "section matter",
        "### Nested",
        "nested matter",
    ]

    assert prose_line_depths(lines) == [0, 1, 1, 2, 2, 3]


def test_indented_prose_does_not_read_as_hierarchy() -> None:
    """A code sample indented under a heading is still that heading's child.

    The tree cutter would read the leading whitespace as depth and cut the
    sample away from the sentence introducing it.
    """
    lines = [
        "## Install",
        "Run this:",
        "    uv sync",
        "    uv run sibyl health",
    ]

    depths = prose_line_depths(lines)

    assert depths[0] == 1
    assert len(set(depths[1:])) <= 2
    assert min(depths[1:]) > depths[0]


def test_a_hash_inside_a_fence_is_not_a_heading() -> None:
    """Reading a shell comment as a heading restructures the whole document."""
    lines = [
        "## Usage",
        "```bash",
        "# install the CLI",
        "uv tool install sibyl",
        "```",
        "after the fence",
    ]

    depths = prose_line_depths(lines)

    assert depths[2] > depths[0]
    assert depths[2] != 0


def test_a_fence_inside_the_band_is_not_cut_in_half() -> None:
    fence = "\n".join(["```python", *[f"line_{index} = {index}" for index in range(12)], "```"])
    body = "\n".join(["# Guide", "", _paragraph("prelude", 120), "", fence, "", "tail sentence"])

    slices, _ = slice_prose(body)
    holding_fence = [entry for entry in slices if "```python" in entry.content]

    assert len(holding_fence) == 1
    assert holding_fence[0].content.count("```") == 2


def test_a_fence_above_the_hard_max_is_descended_and_keeps_its_opener() -> None:
    """Emitting a whole oversize listing as one slice would abandon the band.

    So the descend rule applies to a large fence like any other subtree. The
    reader still has to be able to tell they are looking at fenced content, so
    every piece carries the opening line in its content or its breadcrumb.
    """
    inner = [f"result_{index} = compute({index})  # {_paragraph('note', 6)}" for index in range(40)]
    fence = "\n".join(["```python", *inner, "```"])
    body = "\n".join(["# Guide", "", fence, "", "tail sentence"])

    slices, _ = slice_prose(body)
    pieces = [entry for entry in slices if any(line in entry.content for line in inner)]

    assert len(pieces) > 1, "expected an oversize fence to be descended"
    assert all(len(entry.content) <= HARD_MAX for entry in pieces)
    assert all("```python" in entry.content or "```python" in entry.breadcrumb for entry in pieces)
    assert _partition(body) == list(range(len(body.split("\n"))))


def test_a_paragraph_is_never_split_across_slices() -> None:
    marker = "SENTINEL"
    paragraph_lines = [f"{marker} clause {index} " + _paragraph("filler", 12) for index in range(6)]
    body = "\n".join(["# Doc", "", _paragraph("lead", 120), "", *paragraph_lines])

    slices, _ = slice_prose(body)
    holding = [entry for entry in slices if marker in entry.content]

    assert len(holding) == 1
    assert holding[0].content.count(marker) == len(paragraph_lines)


def test_prose_breadcrumbs_name_the_heading_trail() -> None:
    body = _document()

    slices, _ = slice_prose(body)
    trails = [entry.breadcrumb for entry in slices if entry.breadcrumb]

    assert trails, "expected at least one slice below a heading"
    assert any(trail.startswith("Runbook") for trail in trails)
    assert all("#" not in trail for trail in trails)


def test_prose_packing_stays_inside_the_hard_max_unless_a_line_cannot_be_cut() -> None:
    slices, _ = slice_prose(_document())

    oversize = [entry for entry in slices if len(entry.content) > HARD_MAX]

    assert all(len(entry.line_indices) == 1 for entry in oversize)


def test_prose_slices_reach_the_target_band() -> None:
    """A cutter that emits one slice per paragraph would defeat the substrate."""
    slices, _ = slice_prose(_document())
    body_slices = [entry for entry in slices if entry.content.strip()]

    assert len(body_slices) > 1
    assert max(len(entry.content) for entry in body_slices) >= TARGET_LO


def test_a_document_with_no_headings_still_slices() -> None:
    body = "\n\n".join(_paragraph(f"para{index}", 60) for index in range(8))

    slices, _ = slice_prose(body)

    assert len(slices) > 1
    assert _partition(body) == list(range(len(body.split("\n"))))
