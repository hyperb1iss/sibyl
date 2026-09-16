from resolver import plan
def solve(d):
 return {'reports':{r['name']:plan(r['limit'],[(b['at'],b['until'],b['people']) for b in r['bookings']]) for r in d['rooms']}}
