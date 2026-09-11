def materialize(events):
    """Materialize full-state cache events."""
    latest = {}
    for event in events:
        latest[event["key"]] = event
    return {key: event["value"] for key, event in latest.items() if not event["deleted"]}
