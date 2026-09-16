Repair meter aggregation. Input {readings:[{device,label,units}]}. Each row is a separate observation, even when its label and body repeat; labels are descriptive, not idempotency keys. Sum signed units per device and return {totals}. Never deduplicate.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
