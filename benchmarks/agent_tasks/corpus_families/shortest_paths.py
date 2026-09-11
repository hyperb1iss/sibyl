"""Iterative relaxation and source-reachable negative-cycle discrimination."""

from .model import Family, case, source


def shortest_paths(seed: int) -> Family:
    contract = """# Directed route costs

Read unique string `nodes`, a `source` node, and directed `edges` with from, to,
and integer weight. Parallel edges, self edges, negative weights and arbitrarily
large integers are valid. Return {"distances": {node: minimum cost or null when
unreachable}}. Source starts at cost zero. Any negative cycle reachable from
source invalidates the entire result, even if it cannot reach another node of
interest: return {"error": "negative cycle"}. Unreachable negative cycles do not
invalidate the result. A source or edge endpoint outside nodes returns
{"error": "invalid graph"}. There are no other input size or graph-depth limits.

Edge ordering currently changes distances, and disconnected cycles cause false
alarms. Repair propagation and cycle detection. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from routes import distances
        from cycles import negative_cycle
        def dispatch(request):
            nodes, source, edges = request["nodes"], request["source"], request["edges"]
            if source not in nodes or any(edge["from"] not in nodes or edge["to"] not in nodes for edge in edges):
                return {"error": "invalid graph"}
            result = distances(nodes, source, edges)
            if negative_cycle(result, edges):
                return {"error": "negative cycle"}
            return {"distances": result}
        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    routes = source("""
        def distances(nodes, source, edges):
            result = dict.fromkeys(nodes)
            result[source] = 0
            for _ in range(1):
                changed = False
                for edge in edges:
                    start, end, weight = edge["from"], edge["to"], edge["weight"]
                    if result[start] is not None and (result[end] is None or result[start] + weight < result[end]):
                        result[end] = result[start] + weight
                        changed = True
                if not changed:
                    break
            return result
    """)
    fixed_routes = routes.replace("range(1)", "range(len(nodes) - 1)")
    cycles = source("""
        def negative_cycle(result, edges):
            for edge in edges:
                start = result[edge["from"]]
                end = result[edge["to"]]
                if (start or 0) + edge["weight"] < (end or 0):
                    return True
            return False
    """)
    fixed_cycles = cycles.replace(
        '(start or 0) + edge["weight"] < (end or 0)',
        'start is not None and (end is None or start + edge["weight"] < end)',
    )

    def check(label, nodes, edges, expected, source_node="s"):
        return case(
            label,
            {
                "nodes": nodes,
                "source": source_node,
                "edges": [{"from": a, "to": b, "weight": w} for a, b, w in edges],
            },
            expected,
        )

    return Family(
        "directed-route-costs",
        "learning",
        "signed-directed-shortest-path-v1",
        contract,
        {"app.py": app, "routes.py": routes, "cycles.py": cycles},
        {"routes.py": fixed_routes, "cycles.py": fixed_cycles},
        {"routes.py": fixed_routes},
        [
            check(
                "public-reverse-order",
                ["s", "a", "b", "c"],
                [("b", "c", 2), ("a", "b", 3), ("s", "a", seed + 1)],
                {"distances": {"s": 0, "a": seed + 1, "b": seed + 4, "c": seed + 6}},
            )
        ],
        [
            check(
                "private-unreachable-cycle",
                ["s", "x", "y"],
                [("x", "y", -2), ("y", "x", 1)],
                {"distances": {"s": 0, "x": None, "y": None}},
            ),
            check(
                "private-reachable-cycle",
                ["s", "x", "y", "target"],
                [("s", "target", 4), ("s", "x", 0), ("x", "y", -2), ("y", "x", 1)],
                {"error": "negative cycle"},
            ),
            check(
                "private-negative-acyclic",
                ["s", "a", "b"],
                [("a", "b", -9), ("s", "a", 4), ("s", "b", 1)],
                {"distances": {"s": 0, "a": 4, "b": -5}},
            ),
            check(
                "private-parallel",
                ["s", "a"],
                [("s", "a", 9), ("s", "a", 2), ("s", "a", 8)],
                {"distances": {"s": 0, "a": 2}},
            ),
            check("private-negative-self", ["s"], [("s", "s", -1)], {"error": "negative cycle"}),
            check(
                "private-zero-cycle",
                ["s", "a"],
                [("s", "a", -3), ("a", "s", 3)],
                {"distances": {"s": 0, "a": -3}},
            ),
            check(
                "private-large-weight",
                ["s", "a"],
                [("s", "a", 10**30 + seed)],
                {"distances": {"s": 0, "a": 10**30 + seed}},
            ),
            check(
                "private-invalid-endpoint", ["s"], [("s", "missing", 1)], {"error": "invalid graph"}
            ),
            check("private-invalid-source", ["a"], [], {"error": "invalid graph"}),
            check(
                "private-deep-chain",
                ["s"] + [str(i) for i in range(1200)],
                [("s" if i == 0 else str(i - 1), str(i), 1) for i in reversed(range(1200))],
                {"distances": {"s": 0, **{str(i): i + 1 for i in range(1200)}}},
            ),
        ],
        mechanism_cluster="signed-path-relaxation-reachable-cycle",
    )
