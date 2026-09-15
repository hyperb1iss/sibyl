def slots(limit,ranges):
 load={}
 for a,b,u in ranges:
  if a>b:return None
  for t in range(a,b):load[t]=load.get(t,0)+u
 return {'peak':max(load.values(),default=0),'over':sorted(t for t,v in load.items() if v>limit)}
