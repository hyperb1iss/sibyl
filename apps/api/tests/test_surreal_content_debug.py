import pytest

from sibyl.persistence.surreal import content as content_persistence


def test_scope_content_debug_query_adds_org_filter_before_limit() -> None:
    query = "SELECT * FROM raw_captures LIMIT $limit"

    scoped = content_persistence._scope_content_debug_query(query)

    assert (
        scoped == "SELECT * FROM raw_captures WHERE organization_id = $organization_id LIMIT $limit"
    )


def test_scope_content_debug_query_wraps_existing_where_clause() -> None:
    query = "SELECT * FROM raw_captures WHERE organization_id = $group_id OR true LIMIT 5"

    scoped = content_persistence._scope_content_debug_query(query)

    assert (
        scoped == "SELECT * FROM raw_captures WHERE (organization_id = $group_id OR true) "
        "AND organization_id = $organization_id LIMIT 5"
    )


def test_scope_content_debug_query_rejects_content_table_subqueries() -> None:
    query = """
    SELECT *, (SELECT * FROM raw_captures) AS sibling_rows
    FROM raw_captures
    """

    with pytest.raises(ValueError, match="one content table"):
        content_persistence._scope_content_debug_query(query)
