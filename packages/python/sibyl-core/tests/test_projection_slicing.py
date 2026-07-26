from __future__ import annotations

import sys

import pytest

from sibyl_core.projection.slicing import (
    HARD_MAX,
    detect_indent_unit,
    line_depths,
    render_slice,
    slice_body,
    slice_header,
    uri_path,
)


def _accessibility_tree(indent: str = "\t") -> tuple[str, list[int]]:
    """Render a nested tree deep and wide enough to force descend and packing."""
    lines: list[str] = []
    depths: list[int] = []
    for section in range(6):
        lines.append(f"section 'Section {section}'")
        depths.append(0)
        for group in range(5):
            lines.append(f"{indent}group 'Group {section}.{group}'")
            depths.append(1)
            for row in range(5):
                detail = "detail " * 8
                lines.append(f"{indent * 2}StaticText 'Row {section}.{group}.{row} {detail}'")
                depths.append(2)
    return "\n".join(lines), depths


def _partition(body: str) -> list[int]:
    slices, _ = slice_body(body)
    indices: list[int] = []
    for entry in slices:
        indices.extend(entry.line_indices)
    return indices


def test_slices_partition_the_body_lines_exactly() -> None:
    body, _ = _accessibility_tree()
    line_count = len(body.split("\n"))

    assert _partition(body) == list(range(line_count))


def test_partition_holds_when_blank_lines_break_up_the_tree() -> None:
    body, _ = _accessibility_tree()
    lines: list[str] = []
    for index, line in enumerate(body.split("\n")):
        lines.append(line)
        if index % 17 == 0:
            lines.append("")

    assert _partition("\n".join(lines)) == list(range(len(lines)))


def test_packing_stays_inside_the_hard_max_unless_a_line_cannot_be_cut() -> None:
    body, _ = _accessibility_tree()
    slices, stats = slice_body(body)

    assert len(slices) > 1
    oversized = [entry for entry in slices if len(entry.content) > HARD_MAX]
    assert oversized == []
    assert stats.descend_events > 0


def test_a_sub_floor_tail_is_merged_back_into_the_slice_before_it() -> None:
    body, _ = _accessibility_tree()
    _, stats = slice_body(body)

    assert stats.tail_merges > 0
    assert _partition(body) == list(range(len(body.split("\n"))))


def test_a_childless_line_above_the_hard_max_is_emitted_whole() -> None:
    long_line = "StaticText '" + ("x" * (HARD_MAX + 500)) + "'"
    body = "\n".join(["section 'Section'", f"\t{long_line}", "\tgroup 'Tail'"])
    slices, stats = slice_body(body)

    assert stats.oversize_leaf == 1
    whole = next(entry for entry in slices if entry.reason == "oversize-leaf")
    assert whole.content.strip() == long_line
    assert _partition(body) == [0, 1, 2]


def test_space_indented_bodies_resolve_to_the_same_depths_as_tabs() -> None:
    tabbed, expected_depths = _accessibility_tree()
    spaced, _ = _accessibility_tree(indent="  ")

    assert detect_indent_unit(tabbed.split("\n")) == ("\t", 1)
    assert detect_indent_unit(spaced.split("\n")) == (" ", 2)
    assert line_depths(tabbed.split("\n")) == expected_depths
    assert line_depths(spaced.split("\n")) == expected_depths


def test_a_space_indented_body_slices_instead_of_collapsing() -> None:
    spaced, _ = _accessibility_tree(indent="    ")
    slices, _ = slice_body(spaced)

    assert len(slices) > 1
    assert _partition(spaced) == list(range(len(spaced.split("\n"))))
    assert max(entry.cut_depth for entry in slices) > 0


def test_an_empty_body_yields_no_slices() -> None:
    slices, stats = slice_body("   \n\n  ")

    assert slices == []
    assert stats.descend_events == 0


def test_breadcrumbs_name_every_ancestor_above_the_cut() -> None:
    body, _ = _accessibility_tree()
    slices, _ = slice_body(body)

    descended = [entry for entry in slices if entry.breadcrumb]
    assert descended
    assert descended[0].breadcrumb.startswith("section 'Section ")


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("https://shop.example.test/orders/42", "shop/orders/42"),
        ("https://example.test", "example/"),
        ("/local/path", "/local/path"),
    ],
)
def test_uri_paths_keep_the_host_label_and_the_path(uri: str, expected: str) -> None:
    assert uri_path(uri) == expected


def test_long_uri_paths_are_elided_in_the_middle() -> None:
    rendered = uri_path("https://example.test/" + "segment/" * 20)

    assert "…" in rendered
    assert len(rendered) <= 55


def test_headers_stay_within_the_header_budget_and_drop_a_missing_uri() -> None:
    header = slice_header(4, "https://example.test/orders", 2, 9)
    assert header == "Observation 4 · example/orders · slice 2/9"

    assert slice_header(4, None, 2, 9) == "Observation 4 · slice 2/9"
    assert len(slice_header(4, "https://example.test/" + "x" * 400, 2, 9)) <= 120


def test_rendering_omits_an_empty_breadcrumb_line() -> None:
    assert render_slice("head", "", "body") == "head\nbody"
    assert render_slice("head", "trail", "body") == "head\ntrail\nbody"


def test_deep_nesting_does_not_exhaust_the_interpreter_stack() -> None:
    depth = 2_000
    lines = [f"{'\t' * level}group 'Level {level}'" for level in range(depth)]
    body = "\n".join(lines)

    slices, stats = slice_body(body)

    assert stats.descend_events > sys.getrecursionlimit()
    assert _partition(body) == list(range(depth))
    assert len(slices) > 1
