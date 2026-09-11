"""Exact integer allocation with deterministic remainder distribution."""

from .model import Family, case, source


def apportionment(seed: int) -> Family:
    contract = """# Shared invoice allocator

Read nonnegative integer `total` and nonempty `items` containing unique string
`id` and nonnegative integer `weight`. At least one weight is positive.
Allocate every unit using largest remainders: first floor each exact proportional
share, then give remaining units to descending fractional remainder, breaking
remainder ties by ascending id. Return {"allocations": {id: integer_share}}.
Zero-weight items receive zero. Integers may exceed the exact range of a float.

Invoices sometimes lose units and allocation changes when callers reorder items.
Repair quota computation and remainder distribution. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from quotas import quotas
        from distribute import distribute

        def dispatch(request):
            rows = quotas(request["total"], request["items"])
            return {"allocations": distribute(request["total"], rows)}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    quotas = source("""
        def quotas(total, items):
            weight = sum(item["weight"] for item in items)
            return [(item["id"], round(total * item["weight"] / weight), 0) for item in items]
    """)
    distribute = source("""
        def distribute(total, rows):
            result = {name: amount for name, amount, remainder in rows}
            spare = total - sum(result.values())
            for name, amount, remainder in rows[:spare]:
                result[name] += 1
            return result
    """)
    fixed_quotas = source("""
        def quotas(total, items):
            weight = sum(item["weight"] for item in items)
            return [(item["id"], *divmod(total * item["weight"], weight)) for item in items]
    """)
    fixed_distribute = distribute.replace(
        "rows[:spare]", "sorted(rows, key=lambda row: (-row[2], row[0]))[:spare]"
    )

    def request(total, pairs):
        return {"total": total, "items": [{"id": name, "weight": weight} for name, weight in pairs]}

    large = 2**54 + 1
    return Family(
        "integer-apportionment",
        "learning",
        "invoice-largest-remainder-v1",
        contract,
        {"app.py": app, "quotas.py": quotas, "distribute.py": distribute},
        {"quotas.py": fixed_quotas, "distribute.py": fixed_distribute},
        {"quotas.py": fixed_quotas},
        [
            case("public-tie", request(1, [("a", 1), ("b", 1)]), {"allocations": {"a": 1, "b": 0}}),
            case(
                "public-exact",
                request(3 * (seed + 1), [("a", 1), ("b", 2)]),
                {"allocations": {"a": seed + 1, "b": 2 * (seed + 1)}},
            ),
        ],
        [
            case(
                "private-remainders",
                request(2, [("a", 2), ("b", 1)]),
                {"allocations": {"a": 1, "b": 1}},
            ),
            case(
                "private-order", request(1, [("b", 1), ("a", 1)]), {"allocations": {"a": 1, "b": 0}}
            ),
            case(
                "private-large",
                request(large, [("a", 1), ("b", 1)]),
                {"allocations": {"a": 2**53 + 1, "b": 2**53}},
            ),
            case(
                "private-zero-weight",
                request(2, [("empty", 0), ("b", 1), ("a", 1)]),
                {"allocations": {"empty": 0, "b": 1, "a": 1}},
            ),
            case(
                "private-zero-total",
                request(0, [("a", 1), ("b", 1)]),
                {"allocations": {"a": 0, "b": 0}},
            ),
        ],
        mechanism_cluster="exact-integer-largest-remainder",
    )
