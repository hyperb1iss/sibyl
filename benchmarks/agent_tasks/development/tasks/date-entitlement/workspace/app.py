from datetime import date, datetime


def active_on(start, end, day):
    """Check a calendar-date entitlement."""
    if any(
        not isinstance(value, date) or isinstance(value, datetime) for value in (start, end, day)
    ):
        raise TypeError("expected calendar dates")
    if start > end:
        raise ValueError("reversed entitlement")
    return start <= day < end
