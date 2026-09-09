"""Exact small-set optimization with explicit tie-breaking and large integer weights."""

from .model import Family, case, source


def knapsack(seed: int) -> Family:
    contract = """# Small release payload optimizer

Read nonnegative integer `capacity` and up to 18 items, each with unique string id,
nonnegative integer weight and value. Each item may be selected at most once.
Maximize selected value subject to total weight <= capacity. Break ties by lower
total weight, then fewer selected items, then lexicographically smaller tuples of
input indices. Return {"value": total, "weight": total, "items": [selected_ids]}
in input order. Zero-weight items are valid. Empty selection is allowed.

The 18-item domain permits exact subset optimization for this NP-hard problem;
weights/capacity can be huge integers, so a capacity-sized array is inappropriate.
More than 18 items returns {"error": "too many items"}.

Greedy payloads waste available value, and equal-value selections are unstable.
Repair candidate generation and winner selection. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from candidates import candidates
        from choose import choose

        def dispatch(request):
            if len(request["items"]) > 18:
                return {"error": "too many items"}
            winner = choose(candidates(request["items"], request["capacity"]))
            value, weight, indices = winner
            return {"value": value, "weight": weight, "items": [request["items"][i]["id"] for i in indices]}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    candidates = source("""
        def candidates(items, capacity):
            selected = []
            weight = value = 0
            for index in sorted(range(len(items)), key=lambda i: items[i]["value"], reverse=True):
                item = items[index]
                if weight + item["weight"] <= capacity:
                    selected.append(index)
                    weight += item["weight"]
                    value += item["value"]
            yield value, weight, tuple(sorted(selected))
    """)
    choose = source("""
        def choose(candidates):
            return max(candidates, key=lambda item: item[0])
    """)
    fixed_candidates = source("""
        def candidates(items, capacity):
            for mask in range(1 << len(items)):
                indices = tuple(i for i in range(len(items)) if mask & (1 << i))
                weight = sum(items[i]["weight"] for i in indices)
                if weight <= capacity:
                    yield sum(items[i]["value"] for i in indices), weight, indices
    """)
    fixed_choose = source("""
        def choose(candidates):
            return min(candidates, key=lambda item: (-item[0], item[1], len(item[2]), item[2]))
    """)

    def request(capacity, rows):
        return {
            "capacity": capacity,
            "items": [
                {"id": name, "weight": weight, "value": value} for name, weight, value in rows
            ],
        }

    def answer(value, weight, ids):
        return {"value": value, "weight": weight, "items": ids}

    huge = 10**25 + seed
    return Family(
        "exact-payload-knapsack",
        "learning",
        "small-release-subset-optimizer-v1",
        contract,
        {"app.py": app, "candidates.py": candidates, "choose.py": choose},
        {"candidates.py": fixed_candidates, "choose.py": fixed_choose},
        {"candidates.py": fixed_candidates},
        [
            case(
                "public-greedy-trap",
                request(6, [("large", 5, 8), ("left", 3, 5), ("right", 3, 5)]),
                answer(10, 6, ["left", "right"]),
            )
        ],
        [
            case(
                "private-lower-weight",
                request(5, [("heavy", 5, 7), ("light", 3, 7)]),
                answer(7, 3, ["light"]),
            ),
            case(
                "private-fewer-items",
                request(2, [("a", 1, 2), ("b", 1, 2), ("c", 2, 4)]),
                answer(4, 2, ["c"]),
            ),
            case("private-input-tie", request(1, [("z", 1, 5), ("a", 1, 5)]), answer(5, 1, ["z"])),
            case(
                "private-zero-weight",
                request(0, [("free", 0, 3), ("empty", 0, 0)]),
                answer(3, 0, ["free"]),
            ),
            case(
                "private-huge-weight",
                request(huge, [("a", huge, 8), ("b", huge - 1, 8)]),
                answer(8, huge - 1, ["b"]),
            ),
            case("private-empty", request(0, []), answer(0, 0, [])),
            case(
                "private-domain-boundary",
                request(18, [(str(i), 1, 1) for i in range(18)]),
                answer(18, 18, [str(i) for i in range(18)]),
            ),
            case(
                "private-outside-domain",
                request(19, [(str(i), 1, 1) for i in range(19)]),
                {"error": "too many items"},
            ),
        ],
        mechanism_cluster="zero-one-subset-optimization",
    )
