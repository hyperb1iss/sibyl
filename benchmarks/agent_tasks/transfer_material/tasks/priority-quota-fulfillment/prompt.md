Repair a priority quota fulfiller. Input {available, requests:[{id,need,priority}]}. Lower numeric priority wins; ties ascending id. Give each request up to need from remaining stock before moving to the next. This is sequential priority fulfillment, not proportional allocation. Return {granted:{id:count},remaining}; all amounts nonnegative integers.

Repair the existing program. Run python app.py as a JSON stdin/stdout program; public examples are in public_checks.py. Python standard library only.
