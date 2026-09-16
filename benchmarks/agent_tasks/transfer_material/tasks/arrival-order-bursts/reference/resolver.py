def bursts(ticks,gap):
 out=[]
 for t in ticks:
  if out and 0<=t-out[-1][-1]<=gap:out[-1].append(t)
  else:out.append([t])
 return out
