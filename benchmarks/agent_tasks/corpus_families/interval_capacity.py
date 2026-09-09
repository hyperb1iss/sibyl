"""Sweep resource demand across half-open intervals with simultaneous events."""

from .model import Family, case, source


def interval_capacity(seed: int) -> Family:
    contract = """# Worker capacity planner

Read nonnegative integer `capacity` and `jobs` with integer start/end times and
nonnegative integer units. Jobs occupy [start, end): start is included and end is
excluded. A job with start == end consumes nothing; start > end makes the whole
request invalid. Return {"peak": greatest concurrent units, "overloaded": [...]}
where overloaded is the sorted list of maximal half-open intervals with demand
strictly greater than capacity. Merge adjacent overloaded intervals, even when
the amount of excess changes. Empty input has peak zero and no intervals.
Invalid input returns {"error": "invalid interval"}.

The planner treats non-overlapping jobs as simultaneous and splits continuous
overload into fragments. Repair event construction and the sweep projection.
Run `python public_checks.py` for public examples.
"""
    app = source("""
        import json
        import sys
        from events import events
        from sweep import summarize

        def dispatch(request):
            try:
                return summarize(events(request["jobs"]), request["capacity"])
            except ValueError:
                return {"error": "invalid interval"}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    events = source("""
        def events(jobs):
            result = {}
            for job in jobs:
                if job["start"] > job["end"]:
                    raise ValueError("reversed")
                result[job["start"]] = result.get(job["start"], 0) + job["units"]
            return sorted(result.items())
    """)
    sweep = source("""
        def summarize(events, capacity):
            peak = current = 0
            overloaded = []
            for index, (time, delta) in enumerate(events):
                current += delta
                peak = max(peak, current)
                if index + 1 < len(events) and current > capacity:
                    overloaded.append([time, events[index + 1][0]])
            return {"peak": peak, "overloaded": overloaded}
    """)
    fixed_events = events.replace(
        '        result[job["start"]]',
        '        result[job["end"]] = result.get(job["end"], 0) - job["units"]\n        result[job["start"]]',
    )
    fixed_sweep = sweep.replace(
        "            overloaded.append([time, events[index + 1][0]])",
        "            end = events[index + 1][0]\n            if overloaded and overloaded[-1][1] == time:\n                overloaded[-1][1] = end\n            else:\n                overloaded.append([time, end])",
    )

    def request(capacity, jobs):
        return {
            "capacity": capacity,
            "jobs": [
                {"start": start + seed, "end": end + seed, "units": units}
                for start, end, units in jobs
            ],
        }

    return Family(
        "half-open-capacity-sweep",
        "learning",
        "worker-demand-interval-sweep-v1",
        contract,
        {"app.py": app, "events.py": events, "sweep.py": sweep},
        {"events.py": fixed_events, "sweep.py": fixed_sweep},
        {"events.py": fixed_events},
        [
            case(
                "public-disjoint", request(3, [(0, 2, 3), (2, 4, 3)]), {"peak": 3, "overloaded": []}
            ),
            case(
                "public-overlap",
                request(4, [(0, 3, 3), (1, 2, 2)]),
                {"peak": 5, "overloaded": [[seed + 1, seed + 2]]},
            ),
        ],
        [
            case(
                "private-continuous",
                request(2, [(0, 4, 3), (1, 3, 2)]),
                {"peak": 5, "overloaded": [[seed, seed + 4]]},
            ),
            case(
                "private-empty-interval", request(0, [(1, 1, 100)]), {"peak": 0, "overloaded": []}
            ),
            case("private-reversed", request(4, [(3, 2, 1)]), {"error": "invalid interval"}),
            case(
                "private-simultaneous",
                request(3, [(0, 2, 4), (2, 5, 4)]),
                {"peak": 4, "overloaded": [[seed, seed + 5]]},
            ),
            case(
                "private-zero-units",
                request(2, [(0, 5, 3), (2, 4, 0)]),
                {"peak": 3, "overloaded": [[seed, seed + 5]]},
            ),
            case("private-empty", request(0, []), {"peak": 0, "overloaded": []}),
        ],
        mechanism_cluster="half-open-demand-event-sweep",
    )
