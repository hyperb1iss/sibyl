# Window Overlap

Back-to-back booking windows are being rejected as overlapping. Fix overlaps(left, right) in app.py. Each window is a pair of timezone-aware datetime objects with start strictly before end. Windows are half-open: start is included and end excluded. Touching endpoints do not overlap. Different UTC offsets still describe absolute instants. Raise ValueError for empty/reversed windows or naive datetimes. Preserve inputs.

Run the local checks with:

```sh
python -m unittest discover -s tests -v
```

The repository needs Python 3.13 and no external dependencies.
