def process(balances,commands,ledger=None):
 balances=dict(balances);ledger=dict(ledger or {});results=[]
 for tenant,account,key,amount in commands:
  identity=tenant+'/'+key;target=tenant+'/'+account;body=[account,amount]
  if identity in ledger:
   results.append('duplicate' if ledger[identity]==body else 'conflict');continue
  value=balances.get(target,0)+amount
  if value<0:results.append('rejected');continue
  balances[target]=value;ledger[identity]=body;results.append('applied')
 return {'balances':balances,'ledger':ledger,'results':results}
