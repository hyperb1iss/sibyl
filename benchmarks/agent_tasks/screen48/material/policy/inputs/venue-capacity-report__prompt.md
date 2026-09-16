Repair a venue report. Input {rooms:[{name,limit,bookings:[{at,until,people}]}]}. Return {reports:{name:{peak,windows}|null}}. Each room is independent. Half-open bookings; zero-length bookings empty; any reversed booking invalidates only its room. Windows are maximal adjacent-merged intervals where demand is strictly greater than limit.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
