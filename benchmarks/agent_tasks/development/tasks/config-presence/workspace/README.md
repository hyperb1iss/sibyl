# Config Presence

A deployment options resolver drops explicit false, zero, empty-string, and empty-list overrides. Fix resolve_options in app.py. Only keys present in defaults are supported. An absent override or None inherits the default; every other value is an explicit override. Return a fresh dictionary without changing either input. Keep unknown keys out.

Run the local checks with:

```sh
python -m unittest discover -s tests -v
```

The repository needs Python 3.13 and no external dependencies.
