# Ordered Deltas

A replay job loses counter increments. Fix replay(initial, events) in app.py. Events are ordered deltas, not full snapshots: process every event in input order. A set operation stores its value, increment adds its numeric value to the current number (default zero for an absent key), and remove deletes a key if present. A set may store None. Increment inputs and existing values at increment time are numbers. Operations can repeat for the same key. Return a fresh dictionary without changing initial or the events.

Run the local checks with:

```sh
python -m unittest discover -s tests -v
```

The repository needs Python 3.13 and no external dependencies.
