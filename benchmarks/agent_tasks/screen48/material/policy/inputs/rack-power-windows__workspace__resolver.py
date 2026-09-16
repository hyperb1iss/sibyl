def plan(capacity,jobs):
 events={}
 for start,end,units in jobs:
  if start>end:return None
  if start==end:end=start+1
  events[start]=events.get(start,0)+units
  events[end]=events.get(end,0)-units
 times=sorted(events);load=0;peak=0;bad=[]
 for i,t in enumerate(times):
  load+=events[t];peak=max(peak,load)
  if i+1<len(times) and load>capacity:
   end=times[i+1]
   if bad and bad[-1][1]==t:bad[-1][1]=end
   else:bad.append([t,end])
 return {'peak':peak,'windows':bad}
