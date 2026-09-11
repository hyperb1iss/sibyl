"""Weighted cache eviction requires recency updates on both reads and replacement."""

from .model import Family, case, source


def weighted_lru(seed: int) -> Family:
    contract = """# Weighted LRU cache

Read nonnegative integer `capacity` and ordered `operations`. A put operation has
op:"put", string key, JSON value, and nonnegative integer weight. A get operation
has op:"get" and key. Successful gets touch the entry as most recently used and
append its value to `reads`; missing gets append null without changing recency.

A put whose weight exceeds capacity is completely ignored, even for an existing
key: retain the old value, weight, and recency. Otherwise insert or replace the
entry and touch it as most recently used. Evict least recently used entries until
the sum of stored weights is at most capacity. Replacements count only their new
weight. Zero-weight entries are valid even at zero capacity and participate in
normal eviction order. Return {"reads": [...], "keys": [least_to_most_recent],
"weight": total_stored_weight}. A stored null value is still a cache hit and touch.

Hot entries and recently replaced values are being evicted unexpectedly. Repair
recency maintenance without changing capacity rules. Run `python public_checks.py`.
"""
    app = source("""
        import json
        import sys
        from cache import Cache

        def dispatch(request):
            cache = Cache(request["capacity"])
            reads = []
            for operation in request["operations"]:
                if operation["op"] == "put":
                    cache.put(operation["key"], operation["value"], operation["weight"])
                else:
                    reads.append(cache.get(operation["key"]))
            return {"reads": reads, "keys": list(cache.entries), "weight": cache.weight()}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    cache = source("""
        from collections import OrderedDict

        class Cache:
            def __init__(self, capacity):
                self.capacity = capacity
                self.entries = OrderedDict()

            def weight(self):
                return sum(item[1] for item in self.entries.values())

            def get(self, key):
                if key not in self.entries:
                    return None
                return self.entries[key][0]

            def put(self, key, value, weight):
                if weight > self.capacity:
                    return
                self.entries[key] = (value, weight)
                while self.weight() > self.capacity:
                    self.entries.popitem(last=False)
    """)
    partial = cache.replace(
        "        return self.entries[key][0]",
        "        self.entries.move_to_end(key)\n        return self.entries[key][0]",
    )
    reference = partial.replace(
        "        self.entries[key] = (value, weight)",
        "        self.entries[key] = (value, weight)\n        self.entries.move_to_end(key)",
    )

    def put(key, weight=1, value=None):
        return {"op": "put", "key": key, "value": value, "weight": weight}

    def get(key):
        return {"op": "get", "key": key}

    def request(capacity, operations):
        return {"capacity": capacity, "operations": operations}

    return Family(
        "weighted-lru-replacement",
        "learning",
        "weighted-object-cache-v1",
        contract,
        {"app.py": app, "cache.py": cache},
        {"cache.py": reference},
        {"cache.py": partial},
        [
            case(
                "public-read-touch",
                request(2, [put("a", value=seed), put("b"), get("a"), put("c")]),
                {"reads": [seed], "keys": ["a", "c"], "weight": 2},
            )
        ],
        [
            case(
                "private-replace-touch",
                request(2, [put("a"), put("b"), put("a", value="new"), put("c")]),
                {"reads": [], "keys": ["a", "c"], "weight": 2},
            ),
            case(
                "private-oversize-no-touch",
                request(
                    2, [put("a", value="old"), put("b"), put("a", 3, "new"), put("c"), get("a")]
                ),
                {"reads": [None], "keys": ["b", "c"], "weight": 2},
            ),
            case(
                "private-new-weight",
                request(3, [put("a", 2), put("b", 1), put("a", 1), put("c", 1)]),
                {"reads": [], "keys": ["b", "a", "c"], "weight": 3},
            ),
            case(
                "private-zero-capacity",
                request(0, [put("a", 0, "free"), put("b", 1), get("a")]),
                {"reads": ["free"], "keys": ["a"], "weight": 0},
            ),
            case(
                "private-zero-weight-eviction",
                request(1, [put("a", 0), put("b"), put("c")]),
                {"reads": [], "keys": ["c"], "weight": 1},
            ),
            case(
                "private-null-hit",
                request(2, [put("a"), put("b"), get("a")]),
                {"reads": [None], "keys": ["b", "a"], "weight": 2},
            ),
        ],
        mechanism_cluster="weighted-lru-access-and-replacement",
    )
