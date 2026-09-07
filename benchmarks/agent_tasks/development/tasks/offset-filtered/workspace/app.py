def collect_pages(fetch, page_size):
    """Collect matches from offset windows over a fixed inventory."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    items = []
    offset = 0
    while True:
        page = fetch(offset, page_size)
        items.extend(page["items"])
        offset += len(page["items"])
        if len(page["items"]) < page_size:
            return items
