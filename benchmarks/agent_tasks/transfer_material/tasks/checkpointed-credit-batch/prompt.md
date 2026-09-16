Repair credit batch resumption. Input {checkpoint:{balances,accepted:{tenant/key:[account,delta]}}, batch:[{org,wallet,key,delta}]}. Preserve accepted requests from previous batches. Same-tenant key exact replay duplicates; changed body conflicts. Rejections do not reserve IDs. Return {balances,accepted,status}. No negative balances or new accounts from rejection.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
