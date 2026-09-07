def collect_pages(fetch):
    """Collect a finite cursor-based export."""
    items = []
    cursor = None
    while True:
        page = fetch(cursor)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            return items
