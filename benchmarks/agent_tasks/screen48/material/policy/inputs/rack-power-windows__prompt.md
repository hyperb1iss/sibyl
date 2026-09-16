Repair rack power planning. Input {watts, reservations:[[start,end,draw]]}. Reservations occupy half-open intervals, empty intervals consume nothing. Return {peak,windows} where windows are maximal adjacent-merged intervals above watts, or null for any reversed interval. Draw/watts nonnegative integers; times integers.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
