A deployment options resolver drops explicit false, zero, empty-string, and empty-list overrides. Fix resolve_options in app.py. Only keys present in defaults are supported. An absent override or None inherits the default; every other value is an explicit override. Return a fresh dictionary without changing either input. Keep unknown keys out.

Edit app.py. Run `python -m unittest discover -s tests -v` from the repository root. Python 3.13 and the standard library are sufficient; no installation or network access is needed.
