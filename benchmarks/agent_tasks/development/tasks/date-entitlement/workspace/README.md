# Date Entitlement

An entitlement expires one day early. Fix active_on(start, end, day) in app.py. The service accepts datetime.date values, excluding datetime.datetime, and treats both start and end as included calendar dates. A one-day entitlement is valid. Return whether day is inside the range. Reject reversed ranges with ValueError and any non-date or datetime input with TypeError. Do not apply timestamp half-open conventions to this API.

Run the local checks with:

```sh
python -m unittest discover -s tests -v
```

The repository needs Python 3.13 and no external dependencies.
