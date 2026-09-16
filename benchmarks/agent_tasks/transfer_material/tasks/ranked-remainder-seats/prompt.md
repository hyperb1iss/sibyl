Repair proportional seat allocation. Input {seats, candidates:[{name,weight}], tie_order:[names]}. Names unique; tie_order lists each name exactly once. Positive total weight. Floor exact rational shares then distribute remaining seats by descending exact remainder; ties follow tie_order, not candidate arrival or lexical order. Return {seats:[{name,count}]} in candidate input order. Nonnegative integers may exceed2^53.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
