Repair an asset search projection. Input {updates:[[asset,revision,removed,description]],terms:[query_strings]}. Highest revision wins; later arrival wins ties; removal retains revision against stale resurrection. Query tokens use casefold and whitespace, all must match. Empty queries match all live assets. Return {hits:[sorted_asset_ids,...]}.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
