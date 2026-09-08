"""Reconcile reservation snapshots before deciding available capacity."""

from .model import Family, case, source


def reservations(seed: int) -> Family:
    contract = """# Venue inventory reconciliation

Read `capacity`, `now`, and reservation `events`. Events carry id, revision,
seats, expires, and cancelled. For each id use its greatest revision, breaking
revision ties by the last event in input order. A selected event consumes seats
only when not cancelled and expires is strictly greater than now. A cancelled or
expired revision still supersedes older revisions. Return {"available": capacity
minus consumed seats}, clamped at zero. Inputs contain nonnegative integer seats
and capacity, integer timestamps, and positive revisions.

A feed can arrive out of order. Availability currently changes when an old event
is replayed, and cancellations sometimes leave seats occupied. Repair the reducer
and capacity projection. Run `python public_checks.py` for public examples.
"""
    app = source("""
        import json
        import sys
        from reconcile import current
        from capacity import available

        def dispatch(request):
            return {"available": available(request["capacity"], request["now"], current(request["events"]))}

        if __name__ == "__main__":
            json.dump(dispatch(json.load(sys.stdin)), sys.stdout)
    """)
    reducer = source("""
        def current(events):
            selected = {}
            for event in events:
                if not event["cancelled"]:
                    selected[event["id"]] = event
            return list(selected.values())
    """)
    projection = source("""
        def available(capacity, now, events):
            return max(0, capacity - sum(event["seats"] for event in events if event["expires"] >= now))
    """)
    fixed = source("""
        def current(events):
            selected = {}
            for event in events:
                old = selected.get(event["id"])
                if old is None or event["revision"] >= old["revision"]:
                    selected[event["id"]] = event
            return list(selected.values())
    """)
    correct_projection = projection.replace(
        'event["expires"] >= now', 'not event["cancelled"] and event["expires"] > now'
    )

    def event(revision, seats, *, cancelled=False, expires=100):
        return {
            "id": "booking",
            "revision": revision,
            "seats": seats,
            "cancelled": cancelled,
            "expires": expires,
        }

    def request(events):
        return {"capacity": 20 + seed, "now": 50, "events": events}

    return Family(
        "reservation-reconciliation",
        "development",
        "venue-snapshot-reconciliation-v1",
        contract,
        {"app.py": app, "reconcile.py": reducer, "capacity.py": projection},
        {"reconcile.py": fixed, "capacity.py": correct_projection},
        {"reconcile.py": fixed},
        [case("public-late-old", request([event(2, 3), event(1, 8)]), {"available": 17 + seed})],
        [
            case(
                "private-cancel",
                request([event(1, 8), event(2, 8, cancelled=True)]),
                {"available": 20 + seed},
            ),
            case(
                "private-expiry-boundary",
                request([event(1, 8, expires=50)]),
                {"available": 20 + seed},
            ),
            case(
                "private-expired-supersedes",
                request([event(2, 8, expires=10), event(1, 3)]),
                {"available": 20 + seed},
            ),
            case("private-tie", request([event(2, 8), event(2, 3)]), {"available": 17 + seed}),
            case("private-overbooked", request([event(1, 100 + seed)]), {"available": 0}),
        ],
        mechanism_cluster="newest-revision-active-projection",
    )
