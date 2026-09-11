"""Arrival-time watermarks and event-time session boundaries."""

from .model import Family, case, source


def sessions(seed: int) -> Family:
    contract = """# Session activity exporter

Events arrive in the supplied order and contain `user` and integer `time`.
Before each event, compute the watermark as the greatest previously accepted
timestamp minus `lateness`. Reject an event strictly older than that watermark;
an event exactly on the watermark is accepted. The first event is accepted.
Only accepted events advance the greatest timestamp. Timestamps may be negative.

After filtering, group accepted events separately for each user in timestamp
order. Consecutive events whose distance is at most `gap` belong to one session,
including duplicate timestamps. Output sessions sorted by user then start:
{"sessions": [{"user": ..., "start": ..., "end": ..., "count": ...}],
 "rejected": number}. Gap and lateness are nonnegative integers.

The exporter loses valid boundary events and sometimes produces overlapping
sessions after delayed delivery. Repair filtering and session construction.
Run `python public_checks.py` for the public examples.
"""
    entry = source("""
        import json
        import sys
        from watermark import accepted_events
        from windows import sessions_for

        def export(document):
            accepted, rejected = accepted_events(document["events"], document["lateness"])
            users = sorted({event["user"] for event in accepted})
            sessions = []
            for user in users:
                times = [event["time"] for event in accepted if event["user"] == user]
                sessions.extend(sessions_for(user, times, document["gap"]))
            return {"sessions": sessions, "rejected": rejected}

        if __name__ == "__main__":
            json.dump(export(json.load(sys.stdin)), sys.stdout)
    """)
    watermark = source("""
        def accepted_events(events, lateness):
            accepted = []
            rejected = 0
            greatest = None
            for event in events:
                stamp = event["time"]
                if greatest is not None and stamp <= greatest - lateness:
                    rejected += 1
                    continue
                accepted.append(event)
                greatest = stamp if greatest is None else max(greatest, stamp)
            return accepted, rejected
    """)
    windows = source("""
        def sessions_for(user, times, gap):
            result = []
            for stamp in times:
                if not result or stamp - result[-1]["end"] > gap:
                    result.append({"user": user, "start": stamp, "end": stamp, "count": 1})
                else:
                    result[-1]["end"] = stamp
                    result[-1]["count"] += 1
            return result
    """)
    fixed_watermark = watermark.replace("stamp <= greatest", "stamp < greatest")
    fixed_windows = windows.replace("for stamp in times:", "for stamp in sorted(times):")
    shift = seed * 10

    def request(times, gap=3, lateness=10):
        return {
            "events": [{"user": user, "time": stamp + shift} for user, stamp in times],
            "gap": gap,
            "lateness": lateness,
        }

    def session(user, start, end, count):
        return {"user": user, "start": start + shift, "end": end + shift, "count": count}

    return Family(
        "session-watermarks",
        "learning",
        "event-time-session-export-v1",
        contract,
        {"app.py": entry, "watermark.py": watermark, "windows.py": windows},
        {"watermark.py": fixed_watermark, "windows.py": fixed_windows},
        {"watermark.py": fixed_watermark},
        [
            case(
                "public-equal-watermark",
                request([("a", 2), ("a", 2)], lateness=0),
                {"sessions": [session("a", 2, 2, 2)], "rejected": 0},
            ),
            case(
                "public-gap",
                request([("a", 0), ("a", 3), ("a", 7)]),
                {"sessions": [session("a", 0, 3, 2), session("a", 7, 7, 1)], "rejected": 0},
            ),
        ],
        [
            case(
                "private-late-bridge",
                request([("a", 0), ("a", 6), ("a", 3)]),
                {"sessions": [session("a", 0, 6, 3)], "rejected": 0},
            ),
            case(
                "private-global-watermark",
                request([("a", 8), ("b", 2), ("b", 3)], lateness=5),
                {"sessions": [session("a", 8, 8, 1), session("b", 3, 3, 1)], "rejected": 1},
            ),
            case(
                "private-order-and-negative",
                request([("z", -1), ("a", -4), ("z", -3)], gap=2),
                {"sessions": [session("a", -4, -4, 1), session("z", -3, -1, 2)], "rejected": 0},
            ),
            case("private-empty", request([]), {"sessions": [], "rejected": 0}),
        ],
        mechanism_cluster="event-time-session-finalization",
    )
