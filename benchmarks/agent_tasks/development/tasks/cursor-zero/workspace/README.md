# Cursor Zero

A cursor-based export stops before all pages are read. Fix collect_pages(fetch) in app.py. The first call is fetch(None); each response has items and next_cursor. None is the only terminal next_cursor. Tokens can be integers or strings, including 0 and the empty string. Empty pages can have a next cursor. Preserve item order and duplicates; the service guarantees a finite cursor chain. Do not modify returned page objects.

Run the local checks with:

```sh
python -m unittest discover -s tests -v
```

The repository needs Python 3.13 and no external dependencies.
