def replay(initial, events):
    """Replay ordered counter changes."""
    result = dict(initial)
    latest = {event["key"]: event for event in events}
    for event in latest.values():
        key = event["key"]
        if event["op"] == "set":
            result[key] = event["value"]
        elif event["op"] == "increment":
            result[key] = result.get(key, 0) + event["value"]
        elif event["op"] == "remove":
            result.pop(key, None)
        else:
            raise ValueError("unknown operation")
    return result
