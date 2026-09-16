def fulfill(available,requests):
 granted={r['id']:0 for r in requests}
 for r in sorted(requests,key=lambda r:(r['priority'],r['id'])):
  n=min(available,r['need']);granted[r['id']]=n;available-=n
 return {'granted':granted,'remaining':available}
