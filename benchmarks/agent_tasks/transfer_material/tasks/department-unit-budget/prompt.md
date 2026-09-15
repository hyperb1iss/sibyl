Repair integer budget allocation. Input {units, weights:{department:nonnegative_integer}} with positive weight sum. Return {units_by_department}. Floor exact rational shares, then distribute every remaining unit by descending remainder, ties lexicographic department. No floats; integers can exceed2^53. Zero weights receive zero.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
