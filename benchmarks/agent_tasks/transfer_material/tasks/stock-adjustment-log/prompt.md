Repair stock adjustments. Input {stock:{tenant/sku:integer}, changes:[[tenant,sku,request_id,signed_delta]]}. Successful requests reserve IDs within tenant; exact replay duplicates, changed sku/delta conflicts. Negative resulting stock rejects without reserving ID or creating stock. Return {stock,verdicts}. Process in arrival order.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
