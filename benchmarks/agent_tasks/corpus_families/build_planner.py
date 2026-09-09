"""Dependency closure and deterministic scheduling of requested build targets."""

from .model import Family, case, source


def build_planner(seed: int) -> Family:
    contract = """# Incremental build planner

Read `targets` (mapping names to dependency-name lists) and `requested` names.
Return {"order": [...]}, containing exactly the requested targets and their
transitive dependencies once each. Dependencies must precede dependents. At every
step choose the lexicographically smallest currently ready name. An unknown name
or a cycle in the requested closure returns {"error": "invalid plan"}. Unrequested
cycles or missing references do not invalidate a plan. Empty requests return an
empty order. Duplicate dependency edges and requested names are harmless.

Builds currently omit indirect prerequisites. A previous attempt to expand all
targets also made unrelated broken targets block successful builds. Repair closure
selection and scheduling. Run `python public_checks.py` for public examples.
"""
    app = source("""
        import json
        import sys
        from closure import select
        from schedule import order

        def dispatch(request):
            try:
                return {"order": order(request["targets"], select(request["targets"], request["requested"]))}
            except ValueError:
                return {"error": "invalid plan"}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    closure = source("""
        def select(targets, requested):
            return set(requested)
    """)
    schedule = source("""
        def order(targets, selected):
            return sorted(selected)
    """)
    fixed_closure = source("""
        def select(targets, requested):
            selected = set()
            pending = list(requested)
            while pending:
                name = pending.pop()
                if name in selected:
                    continue
                if name not in targets:
                    raise ValueError("missing target")
                selected.add(name)
                pending.extend(targets[name])
            return selected
    """)
    fixed_schedule = source("""
        def order(targets, selected):
            remaining = set(selected)
            emitted = set()
            result = []
            while remaining:
                ready = sorted(name for name in remaining if set(targets[name]) <= emitted)
                if not ready:
                    raise ValueError("cycle")
                name = ready[0]
                remaining.remove(name)
                emitted.add(name)
                result.append(name)
            return result
    """)
    leaf = f"z-lib-{seed}"
    return Family(
        "build-dependency-planning",
        "development",
        "build-closure-topological-v1",
        contract,
        {"app.py": app, "closure.py": closure, "schedule.py": schedule},
        {"closure.py": fixed_closure, "schedule.py": fixed_schedule},
        {"closure.py": fixed_closure},
        [
            case(
                "public-closure",
                {"targets": {"z": ["a"], "a": []}, "requested": ["z"]},
                {"order": ["a", "z"]},
            )
        ],
        [
            case(
                "private-order",
                {"targets": {"a-app": [leaf], leaf: []}, "requested": ["a-app"]},
                {"order": [leaf, "a-app"]},
            ),
            case(
                "private-cycle",
                {"targets": {"a": ["b"], "b": ["a"]}, "requested": ["a"]},
                {"error": "invalid plan"},
            ),
            case(
                "private-unknown",
                {"targets": {"a": ["missing"]}, "requested": ["a"]},
                {"error": "invalid plan"},
            ),
            case(
                "private-unrelated",
                {"targets": {"a": [], "b": ["b"]}, "requested": ["a", "a"]},
                {"order": ["a"]},
            ),
            case(
                "private-ready-tie",
                {"targets": {"b": [], "a": ["b"], "c": []}, "requested": ["a", "c"]},
                {"order": ["b", "a", "c"]},
            ),
            case(
                "private-duplicate-edges",
                {"targets": {"a": ["b", "b"], "b": []}, "requested": ["a"]},
                {"order": ["b", "a"]},
            ),
        ],
        mechanism_cluster="dependency-closure-topological-order",
    )
