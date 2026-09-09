"""Configuration reduction and post-reduction schema validation."""

from .model import Family, case, source


def configuration(seed: int) -> Family:
    contract = """# Release configuration service

The CLI reads defaults, profiles, and requested names in `order`, then an `override`
object. Apply defaults, each named profile in order, then the override. An absent
profile contributes nothing. Keys present in a later layer replace earlier values,
including false, zero and an empty string. A null value removes a key.

The final configuration must contain nonempty string `service`, boolean `enabled`,
and nonnegative integer `limit` (booleans are not integers here). Only those keys
and optional string `label` are accepted. Validate the final result, not individual
layers. Return {"config": result} or {"error": "invalid configuration"}. The input
layers must remain unchanged. Malformed top-level documents are outside the task.

Operations reports say valid overrides sometimes disappear and combinations of
otherwise reasonable profiles reach deployment with invalid configurations.
Find the interaction between reduction and validation and repair the service.
Run `python public_checks.py` for the public examples.
"""
    entry = source("""
        import json
        import sys
        from layers import resolve
        from schema import valid

        def dispatch(document):
            if not valid(document["defaults"]):
                return {"error": "invalid configuration"}
            merged = resolve(document)
            return {"config": merged}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    layers = source("""
        def overlay(current, changes):
            updated = dict(current)
            for key, value in changes.items():
                if value is None:
                    updated.pop(key, None)
                else:
                    updated[key] = value or updated.get(key)
            return updated

        def resolve(document):
            result = dict(document["defaults"])
            for name in document["order"]:
                result = overlay(result, document["profiles"].get(name, {}))
            return overlay(result, document["override"])
    """)
    schema = source("""
        def valid(configuration):
            allowed = {"service", "enabled", "limit", "label"}
            return (
                not set(configuration).difference(allowed)
                and isinstance(configuration.get("service"), str)
                and bool(configuration["service"])
                and type(configuration.get("enabled")) is bool
                and type(configuration.get("limit")) is int
                and configuration["limit"] >= 0
                and ("label" not in configuration or isinstance(configuration["label"], str))
            )
    """)
    fixed_layers = layers.replace("value or updated.get(key)", "value")
    fixed_entry = entry.replace(
        'if not valid(document["defaults"]):',
        "merged = resolve(document)\n    if not valid(merged):",
    ).replace("    merged = resolve(document)\n    return", "    return")
    defaults = {"service": f"release-{seed}", "enabled": True, "limit": 10 + seed, "label": "old"}

    def document(override, profiles=None, order=None, initial=None):
        return {
            "defaults": initial or defaults,
            "profiles": profiles or {},
            "order": order or [],
            "override": override,
        }

    return Family(
        "layered-configuration",
        "learning",
        "configuration-reducer-v1",
        contract,
        {"app.py": entry, "layers.py": layers, "schema.py": schema},
        {"app.py": fixed_entry, "layers.py": fixed_layers},
        {"layers.py": fixed_layers},
        [
            case(
                "public-falsey",
                document({"enabled": False, "limit": 0, "label": ""}),
                {"config": defaults | {"enabled": False, "limit": 0, "label": ""}},
            ),
            case(
                "public-order",
                document({}, {"one": {"limit": 4}, "two": {"limit": 7}}, ["one", "missing", "two"]),
                {"config": defaults | {"limit": 7}},
            ),
        ],
        [
            case(
                "private-required-deleted",
                document({"service": None}),
                {"error": "invalid configuration"},
            ),
            case(
                "private-final-unknown",
                document({"surprise": 1}),
                {"error": "invalid configuration"},
            ),
            case(
                "private-repaired-layer",
                document({"service": "fixed"}, initial=defaults | {"service": ""}),
                {"config": defaults | {"service": "fixed"}},
            ),
            case(
                "private-remove-label",
                document({"label": None}),
                {"config": {k: v for k, v in defaults.items() if k != "label"}},
            ),
            case(
                "private-bool-limit", document({"limit": False}), {"error": "invalid configuration"}
            ),
        ],
        mechanism_cluster="ordered-layer-validation",
    )
