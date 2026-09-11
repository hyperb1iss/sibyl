# Versioned Events

Out-of-order full-state updates resurrect deleted cache entries. Fix materialize(events) in app.py. Each event has key, integer version, deleted, and value. Select the highest version for each key; equal versions use the last event in input order. A winning deleted event removes the key regardless of its value. Lower-version events must not undo that tombstone. False and None are valid live values. Return a dictionary without changing the input events. These events are full replacements, not deltas.

Run the local checks with:

```sh
python -m unittest discover -s tests -v
```

The repository needs Python 3.13 and no external dependencies.
