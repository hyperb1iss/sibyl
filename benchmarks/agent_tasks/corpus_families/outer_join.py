"""Left outer join retains duplicate multiplicity while NULL keys never match."""

from .model import Family, case, source


def outer_join(seed: int) -> Family:
    contract = """# Customer enrichment join

Read `left` and `right` arrays of rows with unique string id within each array
and optional key (string or null). Produce a left outer equijoin. A missing key
is NULL, and NULL never matches another key, including NULL. Every matching
right row produces one result per left row; unmatched left rows produce one
result with right:null. Return {"rows": [{"left": id, "right": id_or_null}, ...]}.
Preserve left input order and, within each left row, right input order. Duplicate
non-null keys are valid and retain their full multiplicity. Inputs remain unchanged.

Enrichment silently loses duplicate partners and associates unknown customers.
Repair indexing and join expansion. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from lookup import build
        from join import expand

        def dispatch(request):
            return {"rows": expand(request["left"], build(request["right"]))}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    lookup = source("""
        def build(rows):
            index = {}
            for row in rows:
                index[row.get("key")] = [row["id"]]
            return index
    """)
    join = source("""
        def expand(left, index):
            result = []
            for row in left:
                for partner in index.get(row.get("key"), [None]):
                    result.append({"left": row["id"], "right": partner})
            return result
    """)
    partial = lookup.replace(
        'index[row.get("key")] = [row["id"]]',
        'index.setdefault(row.get("key"), []).append(row["id"])',
    )
    reference = partial.replace(
        "        index.setdefault",
        '        if row.get("key") is None:\n            continue\n        index.setdefault',
    )

    def request(left, right):
        return {"left": left, "right": right}

    key = f"customer-{seed}"
    return Family(
        "null-aware-outer-join",
        "learning",
        "customer-multiset-left-join-v1",
        contract,
        {"app.py": app, "lookup.py": lookup, "join.py": join},
        {"lookup.py": reference},
        {"lookup.py": partial},
        [
            case(
                "public-multiplicity",
                request(
                    [{"id": "a", "key": key}], [{"id": "x", "key": key}, {"id": "y", "key": key}]
                ),
                {"rows": [{"left": "a", "right": "x"}, {"left": "a", "right": "y"}]},
            )
        ],
        [
            case(
                "private-null",
                request([{"id": "a", "key": None}], [{"id": "x", "key": None}]),
                {"rows": [{"left": "a", "right": None}]},
            ),
            case(
                "private-missing",
                request([{"id": "a"}], [{"id": "x"}]),
                {"rows": [{"left": "a", "right": None}]},
            ),
            case(
                "private-empty-key",
                request([{"id": "a", "key": ""}], [{"id": "x", "key": ""}]),
                {"rows": [{"left": "a", "right": "x"}]},
            ),
            case(
                "private-unmatched",
                request([{"id": "a", "key": "a"}], [{"id": "x", "key": "b"}]),
                {"rows": [{"left": "a", "right": None}]},
            ),
            case(
                "private-cartesian",
                request(
                    [{"id": "b", "key": key}, {"id": "a", "key": key}],
                    [{"id": "y", "key": key}, {"id": "x", "key": key}],
                ),
                {
                    "rows": [
                        {"left": "b", "right": "y"},
                        {"left": "b", "right": "x"},
                        {"left": "a", "right": "y"},
                        {"left": "a", "right": "x"},
                    ]
                },
            ),
            case("private-empty-left", request([], [{"id": "x", "key": None}]), {"rows": []}),
        ],
        mechanism_cluster="multiset-outer-join-null-semantics",
    )
