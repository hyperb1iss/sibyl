"""Named savepoint stack repair with duplicate-name resolution."""

from .model import Family, case, source


def savepoints(seed: int) -> Family:
    contract = """# Transaction savepoints

Read an initial JSON object `data` and an ordered `ops` array. `set` replaces
key with value; `delete` removes key if present. `save` pushes a named snapshot.
`rollback` restores the snapshot of the most recent matching name, discards all
inner savepoints, and retains that target savepoint for repeated rollback.
`release` discards the most recent matching savepoint and all inner savepoints
without changing data. Duplicate names are allowed and resolve from the top.
An unknown rollback/release name appends "unknown savepoint" to errors, leaves
both data and stack unchanged, and processing continues. Other operations are
well formed. Return {"data": object, "savepoints": names in stack order,
"errors": error strings in operation order}. Values are replaced as whole JSON
values, never mutated in place.

Rollback currently loses its target and release unexpectedly undoes writes.
Repair stack selection and lifecycle semantics. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from transaction import execute
        if __name__ == "__main__":
            json.dump(execute(json.load(sys.stdin)), sys.stdout)
    """)
    lookup = source("""
        def locate(stack, name):
            for index, (label, _) in enumerate(stack):
                if label == name:
                    return index
            raise ValueError("unknown savepoint")
    """)
    fixed_lookup = source("""
        def locate(stack, name):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == name:
                    return index
            raise ValueError("unknown savepoint")
    """)
    transaction = source("""
        from lookup import locate
        def execute(request):
            data, stack, errors = dict(request["data"]), [], []
            for op in request["ops"]:
                action = op["op"]
                if action == "set":
                    data[op["key"]] = op["value"]
                elif action == "delete":
                    data.pop(op["key"], None)
                elif action == "save":
                    stack.append((op["name"], dict(data)))
                else:
                    try:
                        index = locate(stack, op["name"])
                    except ValueError as error:
                        errors.append(str(error))
                        continue
                    data = dict(stack[index][1])
                    stack = stack[:index]
            return {"data": data, "savepoints": [name for name, _ in stack], "errors": errors}
    """)
    fixed_transaction = transaction.replace(
        "            data = dict(stack[index][1])\n            stack = stack[:index]",
        '            if action == "rollback":\n                data = dict(stack[index][1])\n                stack = stack[:index + 1]\n            else:\n                stack = stack[:index]',
    )

    def named(op, name="a"):
        return {"op": op, "name": name}

    def put(value):
        return {"op": "set", "key": "x", "value": value}

    def check(label, ops, value, names, errors=None):
        return case(
            label,
            {"data": {"x": seed}, "ops": ops},
            {"data": {"x": value}, "savepoints": names, "errors": errors or []},
        )

    return Family(
        "named-transaction-savepoints",
        "learning",
        "transaction-savepoint-stack-v1",
        contract,
        {"app.py": app, "lookup.py": lookup, "transaction.py": transaction},
        {"lookup.py": fixed_lookup, "transaction.py": fixed_transaction},
        {"transaction.py": fixed_transaction},
        [
            check(
                "public-rollback-retains",
                [named("save"), put(seed + 1), named("rollback")],
                seed,
                ["a"],
            ),
            check(
                "public-release-keeps-data",
                [named("save"), put(seed + 2), named("release")],
                seed + 2,
                [],
            ),
        ],
        [
            check(
                "private-duplicate-rollback",
                [named("save"), put(seed + 1), named("save"), put(seed + 2), named("rollback")],
                seed + 1,
                ["a", "a"],
            ),
            check(
                "private-duplicate-release",
                [
                    named("save"),
                    put(seed + 1),
                    named("save"),
                    named("release"),
                    put(seed + 2),
                    named("rollback"),
                ],
                seed,
                ["a"],
            ),
            check(
                "private-inner-discard",
                [
                    named("save"),
                    put(seed + 1),
                    named("save", "b"),
                    put(seed + 2),
                    named("rollback"),
                ],
                seed,
                ["a"],
            ),
            check(
                "private-release-inner",
                [named("save"), named("save", "b"), put(seed + 3), named("release")],
                seed + 3,
                [],
            ),
            check(
                "private-repeat-rollback",
                [named("save"), put(seed + 1), named("rollback"), put(seed + 2), named("rollback")],
                seed,
                ["a"],
            ),
            check(
                "private-unknown-keeps-state",
                [
                    named("save"),
                    put(seed + 1),
                    named("release", "missing"),
                    named("rollback", "missing"),
                ],
                seed + 1,
                ["a"],
                ["unknown savepoint", "unknown savepoint"],
            ),
            case(
                "private-delete-nested-value",
                {
                    "data": {"x": {"nested": [seed]}},
                    "ops": [named("save"), {"op": "delete", "key": "x"}, named("rollback")],
                },
                {"data": {"x": {"nested": [seed]}}, "savepoints": ["a"], "errors": []},
            ),
        ],
        mechanism_cluster="named-savepoint-stack-rollback",
    )
