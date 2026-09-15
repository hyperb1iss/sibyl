Repair an immutable revision audit search. Input {records:[[key,revision,deleted,text]], query}. Every historical record is searchable, including deletion annotations; do not reduce to latest state. Return {positions:[zero_based_input_positions]} whose text includes every casefolded whitespace query term. Empty query matches all records.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
