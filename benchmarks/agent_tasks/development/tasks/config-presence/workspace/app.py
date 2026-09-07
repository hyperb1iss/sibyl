def resolve_options(defaults, overrides):
    """Resolve a deployment's supported options."""
    return {key: overrides.get(key) or value for key, value in defaults.items()}
