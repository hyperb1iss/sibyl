"""Maximum matching requires reassignment along arbitrarily long augmenting paths."""

from .model import Family, case, source


def worker_matching(seed: int) -> Family:
    contract = """# Worker assignment counter

Read unique string `workers`, unique string `slots`, and `eligible` mapping each
worker to a list of eligible slot names. Missing workers in eligible have no
edges; duplicate edges are harmless. Ignore eligibility-map entries for workers
not requested, but an unknown slot referenced by a requested worker is invalid.
Return {"assigned": maximum_number_of_simultaneous_assignments}. Each worker and
slot may participate in at most one assignment. Invalid input returns
{"error": "unknown slot"}. The output does not require a particular matching.

Greedy assignment leaves workers idle even when existing assignments can be
rearranged. Chains of 1200 workers are valid and must not depend on the Python
call-stack limit. Repair the matcher without changing the graph-validation contract.
Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from graph import edges
        from matching import maximum

        def dispatch(request):
            try:
                return {"assigned": maximum(edges(request))}
            except ValueError:
                return {"error": "unknown slot"}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    graph = source("""
        def edges(request):
            slots = set(request["slots"])
            result = {}
            for worker in request["workers"]:
                choices = list(dict.fromkeys(request["eligible"].get(worker, [])))
                if any(slot not in slots for slot in choices):
                    raise ValueError("unknown slot")
                result[worker] = choices
            return result
    """)
    matching = source("""
        def maximum(edges):
            used = set()
            for worker, choices in edges.items():
                for slot in choices:
                    if slot not in used:
                        used.add(slot)
                        break
            return len(used)
    """)
    reference = source("""
        from collections import deque

        def maximum(edges):
            owner = {}
            for starting in edges:
                parents = {starting: None}
                pending = deque([(starting, 0)])
                augmented = False
                while pending and not augmented:
                    worker, depth = pending.popleft()
                    for slot in edges[worker]:
                        if slot not in owner:
                            owner[slot] = worker
                            while parents[worker] is not None:
                                worker, slot = parents[worker]
                                owner[slot] = worker
                            augmented = True
                            break
                        previous = owner[slot]
                        if previous not in parents:
                            parents[previous] = (worker, slot)
                            pending.append((previous, depth + 1))
            return len(owner)
    """)
    partial = reference.replace(
        "            for slot in edges[worker]:",
        "            if depth > 1:\n                continue\n            for slot in edges[worker]:",
    )

    def request(workers, slots, eligible):
        return {"workers": workers, "slots": slots, "eligible": eligible}

    count = 4 + seed % 3
    names = [f"w{i}" for i in range(count)]
    slots = [f"s{i}" for i in range(count)]
    chain = {names[i]: slots[i : i + 2] for i in range(count - 1)} | {names[-1]: [slots[0]]}
    deep_count = 1200
    deep_names = [str(i) for i in range(deep_count)]
    deep_chain = {str(i): [str(i), str(i + 1)] for i in range(deep_count - 1)} | {
        str(deep_count - 1): ["0"]
    }
    return Family(
        "augmenting-worker-matching",
        "learning",
        "worker-bipartite-matcher-v1",
        contract,
        {"app.py": app, "graph.py": graph, "matching.py": matching},
        {"matching.py": reference},
        {"matching.py": partial},
        [
            case(
                "public-reassignment",
                request(["a", "b"], ["x", "y"], {"a": ["x", "y"], "b": ["x"]}),
                {"assigned": 2},
            )
        ],
        [
            case("private-long-chain", request(names, slots, chain), {"assigned": count}),
            case(
                "private-deep-chain",
                request(deep_names, deep_names, deep_chain),
                {"assigned": deep_count},
            ),
            case(
                "private-hall-deficiency",
                request(
                    ["a", "b", "c"], ["x", "y", "z"], {"a": ["x"], "b": ["x"], "c": ["y", "z"]}
                ),
                {"assigned": 2},
            ),
            case(
                "private-duplicates",
                request(["a", "b"], ["x"], {"a": ["x", "x"], "b": ["x"]}),
                {"assigned": 1},
            ),
            case("private-unused-map", request([], [], {"unused": ["missing"]}), {"assigned": 0}),
            case(
                "private-invalid", request(["a"], [], {"a": ["missing"]}), {"error": "unknown slot"}
            ),
            case("private-missing-worker", request(["a"], ["x"], {}), {"assigned": 0}),
        ],
        mechanism_cluster="bipartite-augmenting-path-reassignment",
    )
