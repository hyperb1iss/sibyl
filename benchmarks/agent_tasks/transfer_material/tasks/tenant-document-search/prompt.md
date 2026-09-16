Repair independent tenant search. Input {events:[{tenant,key,rev,tombstone,body}], requests:[{tenant,words}]}. Latest revision per tenant/key wins, later arrival wins ties, tombstones retain revisions. Query every normalized whitespace word with casefold; empty words matches all live documents of that tenant. Return {answers:[sorted_keys]}.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
