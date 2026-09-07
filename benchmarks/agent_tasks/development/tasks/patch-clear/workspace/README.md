# Patch Clear

A profile patch endpoint cannot clear optional fields. Fix apply_patch in app.py. This endpoint uses a flat merge-patch contract: None removes a key, an omitted key stays unchanged, and all other values replace or add the key. False, zero and empty containers are valid values. Removing an absent key is harmless. Return a new dictionary and preserve both inputs. Unlike an options resolver, None is a deletion here.

Run the local checks with:

```sh
python -m unittest discover -s tests -v
```

The repository needs Python 3.13 and no external dependencies.
