from resolver import decode
def solve(d):return {'entries':decode([bytes.fromhex(x) for x in d['fragments']])}
