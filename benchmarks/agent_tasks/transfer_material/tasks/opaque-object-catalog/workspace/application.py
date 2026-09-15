from resolver import lookup
def solve(d): return {'values':[lookup(d['objects'],k) for k in d['lookups']]}
