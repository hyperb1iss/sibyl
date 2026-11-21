"""Index-served reads for an id list a read statement may only learn while it runs.

SurrealDB 4.0 plans a statement before it runs, so `uuid IN $ids` scans the
table when `$ids` is bound by a block `LET` (in_clumps covers the lists
Python knows), and an `IN` or an equality on the last column of a compound
index walks the index prefix when its value is bound while the statement
runs.

The fragments here read one row per id instead. An equality on a closure
argument is served by a single-column index only while it is the lookup's one
predicate (a second predicate beside it scans the table), so rows are looked
up by uuid alone and the organization is filtered in the select that reads
them. A ledger row is read by the record key its writer gives it, with no
planner in the way. Each costs about a millisecond per id.

Read statements only. Inside a block that also writes, 4.0.1 scans the table
for every closure lookup: 17.8 s for twelve ids over 9,800 captures, against
56 ms for the single `IN` over a client list. A write guard keeps its `IN`.
"""

from __future__ import annotations


def rows_by_uuid(table: str, ids: str, *, projection: str = "*") -> str:
    """An array of the rows of `table` whose uuid is in the array expression `ids`.

    uuid is unique table-wide, so a row of another organization can come
    back: filter the organization in the select that reads this array.
    """
    return (
        f"array::flatten(array::distinct({ids}).map(|$lookup_id| "
        f"(SELECT {projection} FROM {table} WHERE uuid = $lookup_id)))"
    )


#: The record id of one raw capture source state, inside a closure over
#: `$key` items shaped `{org, id}`. An equality on the ledger's compound
#: identity index, with values bound while the statement runs, walks the
#: organization's whole prefix on SurrealDB 4.0 (33 ms per key at 9,800
#: states); the record key reads the one row.
RAW_CAPTURE_STATE_FOR_KEY = (
    "type::record(string::concat('source_states:', "
    "crypto::sha256(type::string([$key.org, 'raw_capture', $key.id]))))"
)
