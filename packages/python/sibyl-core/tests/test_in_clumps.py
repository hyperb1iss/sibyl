from __future__ import annotations

from sibyl_core.backends.surreal.in_clumps import IN_CLUMP, clumped

_TEMPLATE = "SELECT VALUE uuid FROM raw_captures WHERE uuid IN $ids AND $ids_seen = true"


def test_a_list_at_the_cap_stays_one_clump() -> None:
    values = [f"id-{index}" for index in range(IN_CLUMP)]

    expression, params = clumped(_TEMPLATE, "ids", values)

    assert expression == (
        "array::flatten([(SELECT VALUE uuid FROM raw_captures WHERE uuid IN $ids_0"
        " AND $ids_seen = true)])"
    )
    assert params == {"ids_0": values}


def test_a_list_past_the_cap_splits_into_clumps_of_at_most_the_cap() -> None:
    values = [f"id-{index}" for index in range(2 * IN_CLUMP + 1)]

    expression, params = clumped(_TEMPLATE, "ids", values)

    assert list(params) == ["ids_0", "ids_1", "ids_2"]
    assert [len(clump) for clump in params.values()] == [IN_CLUMP, IN_CLUMP, 1]
    assert [value for clump in params.values() for value in clump] == values
    for name in params:
        assert f"uuid IN ${name} AND" in expression


def test_duplicates_collapse_before_the_split_so_no_value_answers_twice() -> None:
    values = [f"id-{index}" for index in range(IN_CLUMP)] + ["id-0", "id-extra"]

    _, params = clumped(_TEMPLATE, "ids", values)

    flattened = [value for clump in params.values() for value in clump]
    assert len(flattened) == len(set(flattened)) == IN_CLUMP + 1


def test_an_empty_list_still_renders_one_empty_clump() -> None:
    expression, params = clumped(_TEMPLATE, "ids", [])

    assert params == {"ids_0": []}
    assert expression.startswith("array::flatten([(")
