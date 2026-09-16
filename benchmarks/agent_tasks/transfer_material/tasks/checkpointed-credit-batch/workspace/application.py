from resolver import process
def solve(d):
 r=process(d['checkpoint']['balances'],[(x['org'],x['wallet'],x['key'],x['delta']) for x in d['batch']],d['checkpoint']['accepted'])
 return {'balances':r['balances'],'accepted':r['ledger'],'status':r['results']}
