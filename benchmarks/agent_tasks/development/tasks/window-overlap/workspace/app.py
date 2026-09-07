def overlaps(left, right):
    """Check whether two half-open booking windows overlap."""
    for start, end in (left, right):
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise ValueError("expected a nonempty aware window")
    return left[0] <= right[1] and right[0] <= left[1]
