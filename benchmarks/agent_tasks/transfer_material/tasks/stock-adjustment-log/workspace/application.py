from resolver import process
def solve(d):
 r=process(d['stock'],d['changes']);return {'stock':r['balances'],'verdicts':r['results']}
