import pytest

from sibyl.persistence.surreal import content as content_persistence


def test_scope_content_debug_query_adds_org_filter_before_limit() -> None:
    query = "SELECT * FROM raw_captures LIMIT $limit"

    scoped = content_persistence._scope_content_debug_query(query)

    assert (
        scoped == "SELECT * FROM raw_captures WHERE organization_id = $organization_id LIMIT $limit"
    )


def test_scope_content_debug_query_allows_memory_usage_events() -> None:
    query = "SELECT uuid, event_at FROM memory_usage_events ORDER BY event_at DESC LIMIT 1"

    scoped = content_persistence._scope_content_debug_query(query)

    assert (
        scoped == "SELECT uuid, event_at FROM memory_usage_events "
        "WHERE organization_id = $organization_id ORDER BY event_at DESC LIMIT 1"
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


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM raw_captures WHERE true) -- hides organization filter",
        "SELECT * FROM raw_captures WHERE true) // hides organization filter",
        "SELECT * FROM raw_captures WHERE true) /* hides organization filter */",
        "SELECT * FROM raw_captures WHERE true) # hides organization filter",
    ],
)
def test_scope_content_debug_query_rejects_comments(query: str) -> None:
    with pytest.raises(ValueError, match="cannot contain comments"):
        content_persistence._scope_content_debug_query(query)


def test_scope_content_debug_query_allows_comment_markers_inside_strings() -> None:
    query = "SELECT * FROM raw_captures WHERE text = '-- not a comment' LIMIT 5"

    scoped = content_persistence._scope_content_debug_query(query)

    assert (
        scoped == "SELECT * FROM raw_captures WHERE (text = '-- not a comment') "
        "AND organization_id = $organization_id LIMIT 5"
    )
