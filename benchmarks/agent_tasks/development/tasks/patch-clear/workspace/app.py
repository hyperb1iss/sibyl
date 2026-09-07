def apply_patch(current, patch):
    """Apply a flat profile merge patch."""
    result = dict(current)
    for key, value in patch.items():
        if value is not None:
            result[key] = value
    return result
