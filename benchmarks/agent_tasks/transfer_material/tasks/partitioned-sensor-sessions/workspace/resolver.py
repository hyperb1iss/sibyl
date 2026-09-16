def sessions(events,gap,lateness,partitioned=False):
 maxima={};accepted={};dropped=0
 for user,t in events:
  partition='*'
  prior=maxima.get(partition)
  if prior is not None and t<prior-lateness:dropped+=1;continue
  maxima[partition]=t if prior is None else max(prior,t)
  accepted.setdefault(user,[]).append(t)
 result=[]
 for user,times in sorted(accepted.items()):
  for t in sorted(times):
   if result and result[-1][0]==user and t-result[-1][2]<=gap:
    result[-1][2]=t;result[-1][3]+=1
   else:result.append([user,t,t,1])
 return result,dropped
